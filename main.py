#!/usr/bin/env python3
"""
Subix Studio - V2Ray Subscription Testing Suite
A self-contained SaaS-level web application for bulk V2Ray subscription management and testing.
"""

import os
import sys
import json
import time
import base64
import sqlite3
import hashlib
import threading
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, asdict
from contextlib import contextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, unquote
import re
import io

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from python_v2ray.config_parser import parse_uri, load_configs, deduplicate_configs, ConfigParams
    from python_v2ray.tester import ConnectionTester
    from python_v2ray.downloader import BinaryDownloader
    HAS_V2RAY_LIB = True
except ImportError:
    HAS_V2RAY_LIB = False
    print("Warning: python_v2ray library not found. Testing features disabled.")

DB_PATH = PROJECT_ROOT / "subix_studio.db"
VENDOR_PATH = PROJECT_ROOT / "vendor"
CORE_ENGINE_PATH = PROJECT_ROOT / "core_engine"

@dataclass
class AppSettings:
    auto_mode: bool = True
    engine_mode: bool = False
    test_interval_minutes: int = 30
    max_concurrent_tests: int = 10
    test_timeout_seconds: int = 15
    failure_threshold: int = 3
    auto_deduplicate: bool = True
    auto_test_new_configs: bool = True
    speed_test_bytes: int = 100000
    dark_mode: bool = True

class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        self.init_db()

    def get_conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    @contextmanager
    def transaction(self):
        conn = self.get_conn()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e

    def init_db(self):
        with self.transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    enabled INTEGER DEFAULT 1,
                    last_updated TEXT,
                    config_count INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER,
                    uri TEXT NOT NULL,
                    uri_hash TEXT NOT NULL,
                    protocol TEXT,
                    address TEXT,
                    port INTEGER,
                    display_tag TEXT,
                    security TEXT,
                    network TEXT,
                    status TEXT DEFAULT 'untested',
                    last_ping_ms INTEGER,
                    last_speed_mbps REAL,
                    consecutive_failures INTEGER DEFAULT 0,
                    total_tests INTEGER DEFAULT 0,
                    successful_tests INTEGER DEFAULT 0,
                    last_tested TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    extra_data TEXT,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );
                
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                
                CREATE TABLE IF NOT EXISTS test_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_id INTEGER,
                    test_type TEXT,
                    result TEXT,
                    ping_ms INTEGER,
                    speed_mbps REAL,
                    tested_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (config_id) REFERENCES configs(id) ON DELETE CASCADE
                );
                
                CREATE INDEX IF NOT EXISTS idx_configs_status ON configs(status);
                CREATE INDEX IF NOT EXISTS idx_configs_uri_hash ON configs(uri_hash);
                CREATE INDEX IF NOT EXISTS idx_configs_subscription ON configs(subscription_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_configs_unique_uri ON configs(uri_hash);
            """)

    def execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.get_conn().execute(query, params)

    def executemany(self, query: str, params_list: List[tuple]):
        return self.get_conn().executemany(query, params_list)

    def fetchone(self, query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self.execute(query, params).fetchone()

    def fetchall(self, query: str, params: tuple = ()) -> List[sqlite3.Row]:
        return self.execute(query, params).fetchall()

    def commit(self):
        self.get_conn().commit()

db = Database(DB_PATH)

class ConfigManager:
    @staticmethod
    def hash_uri(uri: str) -> str:
        normalized = uri.split('#')[0].strip()
        return hashlib.sha256(normalized.encode()).hexdigest()[:32]

    @staticmethod
    def parse_config_details(uri: str) -> Dict[str, Any]:
        if HAS_V2RAY_LIB:
            try:
                params = parse_uri(uri)
                if params:
                    return {
                        'protocol': params.protocol,
                        'address': params.address,
                        'port': params.port,
                        'display_tag': params.display_tag,
                        'security': getattr(params, 'security', ''),
                        'network': getattr(params, 'network', ''),
                        'extra_data': json.dumps(asdict(params) if hasattr(params, '__dataclass_fields__') else {})
                    }
            except:
                pass
        
        protocol = uri.split('://')[0] if '://' in uri else 'unknown'
        tag = uri.split('#')[-1] if '#' in uri else 'Unnamed'
        return {
            'protocol': protocol,
            'address': '',
            'port': 0,
            'display_tag': unquote(tag),
            'security': '',
            'network': '',
            'extra_data': '{}'
        }

    @staticmethod
    def add_config(uri: str, subscription_id: Optional[int] = None) -> Optional[int]:
        uri = uri.strip()
        if not uri or not '://' in uri:
            return None
        
        uri_hash = ConfigManager.hash_uri(uri)
        existing = db.fetchone("SELECT id FROM configs WHERE uri_hash = ?", (uri_hash,))
        if existing:
            return existing['id']
        
        details = ConfigManager.parse_config_details(uri)
        
        with db.transaction() as conn:
            cursor = conn.execute("""
                INSERT INTO configs (subscription_id, uri, uri_hash, protocol, address, port, 
                                    display_tag, security, network, extra_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (subscription_id, uri, uri_hash, details['protocol'], details['address'],
                  details['port'], details['display_tag'], details['security'],
                  details['network'], details['extra_data']))
            return cursor.lastrowid

    @staticmethod
    def add_configs_bulk(uris: List[str], subscription_id: Optional[int] = None) -> int:
        added = 0
        for uri in uris:
            if ConfigManager.add_config(uri, subscription_id):
                added += 1
        return added

    @staticmethod
    def get_all_configs(status_filter: Optional[str] = None, search: str = '', 
                        sort_by: str = 'id', sort_order: str = 'desc',
                        subscription_id: Optional[int] = None) -> List[Dict]:
        query = "SELECT * FROM configs WHERE 1=1"
        params = []
        
        if status_filter:
            if status_filter == 'working':
                query += " AND status = 'working'"
            elif status_filter == 'retired':
                query += " AND status = 'retired'"
            elif status_filter == 'failed':
                query += " AND status = 'failed'"
        
        if subscription_id:
            query += " AND subscription_id = ?"
            params.append(subscription_id)
        
        if search:
            query += " AND (display_tag LIKE ? OR address LIKE ? OR protocol LIKE ? OR uri LIKE ?)"
            search_term = f"%{search}%"
            params.extend([search_term, search_term, search_term, search_term])
        
        valid_sorts = ['id', 'display_tag', 'protocol', 'address', 'port', 'status', 
                       'last_ping_ms', 'last_speed_mbps', 'last_tested', 'created_at']
        if sort_by not in valid_sorts:
            sort_by = 'id'
        
        sort_order = 'ASC' if sort_order.lower() == 'asc' else 'DESC'
        
        if sort_by == 'last_ping_ms':
            query += f" ORDER BY CASE WHEN last_ping_ms IS NULL THEN 1 ELSE 0 END, last_ping_ms {sort_order}"
        else:
            query += f" ORDER BY {sort_by} {sort_order}"
        
        rows = db.fetchall(query, tuple(params))
        return [dict(row) for row in rows]

    @staticmethod
    def update_config_status(config_id: int, status: str, ping_ms: Optional[int] = None,
                             speed_mbps: Optional[float] = None):
        config = db.fetchone("SELECT * FROM configs WHERE id = ?", (config_id,))
        if not config:
            return
        
        consecutive_failures = config['consecutive_failures']
        total_tests = config['total_tests'] + 1
        successful_tests = config['successful_tests']
        
        if status == 'working':
            consecutive_failures = 0
            successful_tests += 1
            final_status = 'working'
        else:
            consecutive_failures += 1
            settings = SettingsManager.get_settings()
            if consecutive_failures >= settings.failure_threshold:
                final_status = 'retired'
            else:
                final_status = 'failed'
        
        with db.transaction() as conn:
            conn.execute("""
                UPDATE configs SET 
                    status = ?, last_ping_ms = ?, last_speed_mbps = ?,
                    consecutive_failures = ?, total_tests = ?, successful_tests = ?,
                    last_tested = ?
                WHERE id = ?
            """, (final_status, ping_ms, speed_mbps, consecutive_failures, 
                  total_tests, successful_tests, datetime.now().isoformat(), config_id))
            
            conn.execute("""
                INSERT INTO test_history (config_id, test_type, result, ping_ms, speed_mbps)
                VALUES (?, 'connection', ?, ?, ?)
            """, (config_id, final_status, ping_ms, speed_mbps))

    @staticmethod
    def delete_configs(config_ids: List[int]):
        if not config_ids:
            return
        placeholders = ','.join(['?' for _ in config_ids])
        with db.transaction() as conn:
            conn.execute(f"DELETE FROM configs WHERE id IN ({placeholders})", tuple(config_ids))

    @staticmethod
    def restore_configs(config_ids: List[int]):
        if not config_ids:
            return
        placeholders = ','.join(['?' for _ in config_ids])
        with db.transaction() as conn:
            conn.execute(f"""
                UPDATE configs SET status = 'untested', consecutive_failures = 0 
                WHERE id IN ({placeholders})
            """, tuple(config_ids))

    @staticmethod
    def retire_working_configs():
        with db.transaction() as conn:
            conn.execute("UPDATE configs SET status = 'retired' WHERE status = 'working'")

class SubscriptionManager:
    @staticmethod
    def add_subscription(name: str, url: str) -> Optional[int]:
        try:
            with db.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO subscriptions (name, url) VALUES (?, ?)",
                    (name, url)
                )
                return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None

    @staticmethod
    def get_all_subscriptions() -> List[Dict]:
        rows = db.fetchall("SELECT * FROM subscriptions ORDER BY created_at DESC")
        return [dict(row) for row in rows]

    @staticmethod
    def update_subscription(sub_id: int):
        sub = db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
        if not sub:
            return {'success': False, 'error': 'Subscription not found'}
        
        try:
            req = urllib.request.Request(sub['url'], headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=30) as response:
                content = response.read()
            
            try:
                content_str = content.decode('utf-8')
            except:
                content_str = content.decode('latin-1')
            
            uris = []
            try:
                decoded = base64.b64decode(content_str).decode('utf-8')
                uris = [line.strip() for line in decoded.split('\n') if line.strip() and '://' in line]
            except:
                uris = [line.strip() for line in content_str.split('\n') if line.strip() and '://' in line]
            
            added = 0
            for uri in uris:
                if ConfigManager.add_config(uri, sub_id):
                    added += 1
            
            with db.transaction() as conn:
                conn.execute("""
                    UPDATE subscriptions SET last_updated = ?, config_count = 
                    (SELECT COUNT(*) FROM configs WHERE subscription_id = ?)
                    WHERE id = ?
                """, (datetime.now().isoformat(), sub_id, sub_id))
            
            return {'success': True, 'added': added, 'total': len(uris)}
        
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @staticmethod
    def delete_subscription(sub_id: int, delete_configs: bool = False):
        with db.transaction() as conn:
            if delete_configs:
                conn.execute("DELETE FROM configs WHERE subscription_id = ?", (sub_id,))
            else:
                conn.execute("UPDATE configs SET subscription_id = NULL WHERE subscription_id = ?", (sub_id,))
            conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))

class SettingsManager:
    @staticmethod
    def get_settings() -> AppSettings:
        settings = AppSettings()
        rows = db.fetchall("SELECT key, value FROM settings")
        for row in rows:
            if hasattr(settings, row['key']):
                val = row['value']
                field_type = type(getattr(settings, row['key']))
                if field_type == bool:
                    setattr(settings, row['key'], val.lower() == 'true')
                elif field_type == int:
                    setattr(settings, row['key'], int(val))
                elif field_type == float:
                    setattr(settings, row['key'], float(val))
                else:
                    setattr(settings, row['key'], val)
        return settings

    @staticmethod
    def save_settings(settings: Dict[str, Any]):
        with db.transaction() as conn:
            for key, value in settings.items():
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, str(value))
                )

class TestRunner:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.is_testing = False
        self.test_progress = {'current': 0, 'total': 0, 'status': 'idle', 'configs': []}
        self.tester = None
        self._init_tester()

    def _init_tester(self):
        if HAS_V2RAY_LIB:
            try:
                downloader = BinaryDownloader(PROJECT_ROOT)
                downloader.ensure_all()
                self.tester = ConnectionTester(
                    vendor_path=str(VENDOR_PATH),
                    core_engine_path=str(CORE_ENGINE_PATH)
                )
            except Exception as e:
                print(f"Failed to initialize tester: {e}")
                self.tester = None

    def test_configs(self, config_ids: List[int] = None, test_type: str = 'connection'):
        if not self.tester:
            return {'success': False, 'error': 'Testing disabled: python_v2ray library missing'}

        if self.is_testing:
            return {'success': False, 'error': 'Test already in progress'}
        
        self.is_testing = True
        threading.Thread(target=self._run_tests, args=(config_ids, test_type), daemon=True).start()
        return {'success': True, 'message': 'Testing started'}

    def stop_tests(self):
        if self.is_testing:
            self.is_testing = False
            return {'success': True, 'message': 'Stopping tests...'}
        return {'success': False, 'error': 'No test running'}

    def _run_tests(self, config_ids: List[int] = None, test_type: str = 'connection'):
        try:
            if config_ids:
                placeholders = ','.join(['?' for _ in config_ids])
                configs = db.fetchall(f"SELECT * FROM configs WHERE id IN ({placeholders})", tuple(config_ids))
                configs = [dict(c) for c in configs]
                import random
                random.shuffle(configs)
            else:
                # Prioritize untested configs, then shuffle the rest to distribute load
                untested = db.fetchall("SELECT * FROM configs WHERE status = 'untested' AND status != 'retired'")
                others = db.fetchall("SELECT * FROM configs WHERE status != 'untested' AND status != 'retired' ORDER BY last_tested ASC")
                
                untested = [dict(c) for c in untested]
                others = [dict(c) for c in others]
                
                import random
                random.shuffle(others)
                configs = untested + others
            
            self.test_progress = {
                'current': 0, 
                'total': len(configs), 
                'status': 'testing',
                'configs': [{
                    'id': c['id'],
                    'name': c['display_tag'] or 'Unnamed',
                    'protocol': c['protocol'],
                    'status': 'pending',
                    'result': None,
                    'ping': None
                } for c in configs]
            }
            
            # Create a map for quick index lookup
            progress_map = {c['id']: i for i, c in enumerate(configs)}
            settings = SettingsManager.get_settings()
            
            if self.tester and HAS_V2RAY_LIB:
                batch_size = settings.max_concurrent_tests
                
                # Process in batches to reduce memory usage and improve responsiveness
                for i in range(0, len(configs), batch_size):
                    if not self.is_testing:
                        break
                        
                    batch_configs = configs[i:i+batch_size]
                    parsed_batch = []
                    config_map = {}
                    
                    # Parse current batch
                    for config in batch_configs:
                        idx = progress_map[config['id']]
                        try:
                            parsed = parse_uri(config['uri'])
                            if parsed:
                                unique_tag = f"test_{config['id']}_{int(time.time()*1000)}"
                                
                                if hasattr(parsed, 'display_tag'): parsed.display_tag = unique_tag
                                if hasattr(parsed, 'tag'): parsed.tag = unique_tag
                                if hasattr(parsed, 'ps'): parsed.ps = unique_tag
                                
                                parsed_batch.append(parsed)
                                config_map[unique_tag] = (config['id'], idx)
                            else:
                                raise Exception("Parse failed")
                        except:
                            ConfigManager.update_config_status(config['id'], 'failed')
                            self.test_progress['configs'][idx]['status'] = 'completed'
                            self.test_progress['configs'][idx]['result'] = 'failed'
                            self.test_progress['current'] += 1
                    
                    if not parsed_batch:
                        continue
                        
                    # Mark as testing
                    for tag in config_map:
                        _, idx = config_map[tag]
                        self.test_progress['configs'][idx]['status'] = 'testing'
                    
                    try:
                        # First pass
                        results = self.tester.test_uris(
                            parsed_params=parsed_batch,
                            timeout=settings.test_timeout_seconds
                        ) or []
                        
                        success_tags = set()
                        for result in results:
                            tag = result.get('tag', '')
                            if result.get('status') == 'success':
                                success_tags.add(tag)
                                if tag in config_map:
                                    cid, idx = config_map[tag]
                                    ConfigManager.update_config_status(cid, 'working', ping_ms=result.get('ping_ms'))
                                    self.test_progress['configs'][idx]['status'] = 'completed'
                                    self.test_progress['configs'][idx]['result'] = 'success'
                                    self.test_progress['configs'][idx]['ping'] = result.get('ping_ms')
                                    self.test_progress['current'] += 1

                        # Retry failed items once
                        retry_batch = [p for p in parsed_batch if 
                                     (getattr(p, 'display_tag', None) or getattr(p, 'tag', None) or getattr(p, 'ps', None)) not in success_tags]
                        
                        if retry_batch:
                            time.sleep(0.5)
                            retry_results = self.tester.test_uris(
                                parsed_params=retry_batch,
                                timeout=settings.test_timeout_seconds
                            ) or []
                            
                            for result in retry_results:
                                tag = result.get('tag', '')
                                if tag in config_map:
                                    cid, idx = config_map[tag]
                                    if self.test_progress['configs'][idx]['status'] == 'testing':
                                        if result.get('status') == 'success':
                                            ConfigManager.update_config_status(cid, 'working', ping_ms=result.get('ping_ms'))
                                            self.test_progress['configs'][idx]['status'] = 'completed'
                                            self.test_progress['configs'][idx]['result'] = 'success'
                                            self.test_progress['configs'][idx]['ping'] = result.get('ping_ms')
                                        else:
                                            ConfigManager.update_config_status(cid, 'failed')
                                            self.test_progress['configs'][idx]['status'] = 'completed'
                                            self.test_progress['configs'][idx]['result'] = 'failed'
                                        self.test_progress['current'] += 1
                        
                        # Cleanup stuck items
                        for tag in config_map:
                            _, idx = config_map[tag]
                            if self.test_progress['configs'][idx]['status'] == 'testing':
                                cid = config_map[tag][0]
                                ConfigManager.update_config_status(cid, 'failed')
                                self.test_progress['configs'][idx]['status'] = 'completed'
                                self.test_progress['configs'][idx]['result'] = 'failed'
                                self.test_progress['current'] += 1
                                
                    except Exception as e:
                        print(f"Batch test error: {e}")
                        for tag in config_map:
                            _, idx = config_map[tag]
                            if self.test_progress['configs'][idx]['status'] == 'testing':
                                self.test_progress['configs'][idx]['status'] = 'completed'
                                self.test_progress['configs'][idx]['result'] = 'error'
                                self.test_progress['current'] += 1
            
            self.test_progress['status'] = 'completed'
        except Exception as e:
            self.test_progress['status'] = f'error: {str(e)}'
        finally:
            self.is_testing = False

class BackgroundScheduler:
    def __init__(self):
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def _run(self):
        last_test_time = 0
        engine_mode_running = False
        
        while self.running:
            try:
                settings = SettingsManager.get_settings()
                
                if settings.engine_mode:
                    if not engine_mode_running:
                        print("[Engine Mode] Starting continuous testing...")
                        engine_mode_running = True
                    
                    runner = TestRunner()
                    if not runner.is_testing:
                        untested = db.fetchall("SELECT id FROM configs WHERE status = 'untested' LIMIT 50")
                        if untested:
                            config_ids = [c['id'] for c in untested]
                            runner.test_configs(config_ids)
                            time.sleep(5)
                        else:
                            working_configs = db.fetchall("SELECT id FROM configs WHERE status = 'working' LIMIT 50")
                            if working_configs:
                                config_ids = [c['id'] for c in working_configs]
                                runner.test_configs(config_ids)
                                time.sleep(5)
                            else:
                                time.sleep(30)
                    else:
                        time.sleep(5)
                
                elif settings.auto_mode:
                    engine_mode_running = False
                    interval = settings.test_interval_minutes * 60
                    current_time = time.time()
                    
                    if current_time - last_test_time >= interval:
                        runner = TestRunner()
                        if not runner.is_testing:
                            working_configs = db.fetchall(
                                "SELECT id FROM configs WHERE status = 'working'"
                            )
                            if working_configs:
                                config_ids = [c['id'] for c in working_configs]
                                runner.test_configs(config_ids)
                                last_test_time = current_time
                    time.sleep(60)
                else:
                    engine_mode_running = False
                    time.sleep(60)
                    
            except Exception as e:
                print(f"Scheduler error: {e}")
                time.sleep(60)

def generate_qr_code_svg(data: str, size: int = 200) -> str:
    try:
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        
        matrix = qr.get_matrix()
        module_count = len(matrix)
        module_size = size / module_count
        
        svg_parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">']
        svg_parts.append(f'<rect width="{size}" height="{size}" fill="white"/>')
        
        for row_idx, row in enumerate(matrix):
            for col_idx, cell in enumerate(row):
                if cell:
                    x = col_idx * module_size
                    y = row_idx * module_size
                    svg_parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{module_size:.2f}" height="{module_size:.2f}" fill="black"/>')
        
        svg_parts.append('</svg>')
        return ''.join(svg_parts)
    except ImportError:
        return generate_simple_qr_svg(data, size)

def generate_simple_qr_svg(data: str, size: int = 200) -> str:
    data_hash = hashlib.md5(data.encode()).hexdigest()
    grid_size = 25
    module_size = size / grid_size
    
    svg_parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">']
    svg_parts.append(f'<rect width="{size}" height="{size}" fill="white"/>')
    
    def draw_finder(x, y):
        s = module_size
        svg_parts.append(f'<rect x="{x*s}" y="{y*s}" width="{7*s}" height="{7*s}" fill="black"/>')
        svg_parts.append(f'<rect x="{(x+1)*s}" y="{(y+1)*s}" width="{5*s}" height="{5*s}" fill="white"/>')
        svg_parts.append(f'<rect x="{(x+2)*s}" y="{(y+2)*s}" width="{3*s}" height="{3*s}" fill="black"/>')
    
    draw_finder(0, 0)
    draw_finder(grid_size - 7, 0)
    draw_finder(0, grid_size - 7)
    
    for i, char in enumerate(data_hash):
        val = int(char, 16)
        row = 8 + (i // 10)
        col = 8 + (i % 10)
        if row < grid_size - 8 and col < grid_size - 8:
            if val > 7:
                svg_parts.append(f'<rect x="{col*module_size}" y="{row*module_size}" width="{module_size}" height="{module_size}" fill="black"/>')
    
    svg_parts.append('</svg>')
    return ''.join(svg_parts)

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Subix Studio</title>
    <style>
        :root {
            --bg-primary: #0a0a0f;
            --bg-secondary: #12121a;
            --bg-tertiary: #1a1a25;
            --bg-hover: #252535;
            --border-color: #2a2a3a;
            --text-primary: #ffffff;
            --text-secondary: #a0a0b0;
            --text-muted: #606070;
            --accent: #6366f1;
            --accent-hover: #818cf8;
            --accent-glow: rgba(99, 102, 241, 0.3);
            --success: #22c55e;
            --warning: #f59e0b;
            --danger: #ef4444;
            --info: #3b82f6;
        }
        
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            overflow: hidden;
        }
        
        .app-container {
            display: flex;
            height: 100vh;
        }
        
        .sidebar {
            width: 240px;
            background: var(--bg-secondary);
            border-right: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
        }
        
        .logo {
            padding: 24px 20px;
            border-bottom: 1px solid var(--border-color);
        }
        
        .logo h1 {
            font-size: 20px;
            font-weight: 700;
            background: linear-gradient(135deg, var(--accent), #a855f7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .logo span {
            font-size: 11px;
            color: var(--text-muted);
            display: block;
            margin-top: 4px;
        }
        
        .nav-menu {
            padding: 16px 12px;
            flex: 1;
        }
        
        .nav-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px 16px;
            border-radius: 8px;
            cursor: pointer;
            color: var(--text-secondary);
            transition: all 0.2s;
            margin-bottom: 4px;
            font-size: 14px;
        }
        
        .nav-item:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }
        
        .nav-item.active {
            background: var(--accent);
            color: white;
            box-shadow: 0 4px 12px var(--accent-glow);
        }
        
        .nav-item .icon {
            width: 20px;
            height: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        .nav-item .badge {
            margin-left: auto;
            background: var(--bg-tertiary);
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 11px;
            color: var(--text-muted);
        }
        
        .nav-item.active .badge {
            background: rgba(255,255,255,0.2);
            color: white;
        }
        
        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            min-width: 0;
            overflow: hidden;
        }
        
        .header {
            height: 64px;
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            padding: 0 24px;
            gap: 16px;
            flex-shrink: 0;
        }
        
        .search-box {
            flex: 1;
            max-width: 400px;
            position: relative;
        }
        
        .search-box input {
            width: 100%;
            padding: 10px 16px 10px 40px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-primary);
            font-size: 14px;
            outline: none;
            transition: all 0.2s;
        }
        
        .search-box input:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px var(--accent-glow);
        }
        
        .search-box::before {
            content: "⌕";
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--text-muted);
            font-size: 16px;
        }
        
        .header-actions {
            display: flex;
            gap: 8px;
            margin-left: auto;
        }
        
        .btn {
            padding: 10px 16px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            border: none;
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        
        .btn-primary {
            background: var(--accent);
            color: white;
        }
        
        .btn-primary:hover {
            background: var(--accent-hover);
            box-shadow: 0 4px 12px var(--accent-glow);
        }
        
        .btn-secondary {
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
        }
        
        .btn-secondary:hover {
            background: var(--bg-hover);
        }
        
        .btn-danger {
            background: var(--danger);
            color: white;
        }
        
        .btn-danger:hover {
            background: #dc2626;
        }
        
        .btn-success {
            background: var(--success);
            color: white;
        }
        
        .content-wrapper {
            display: flex;
            flex: 1;
            overflow: hidden;
        }
        
        .content-area {
            flex: 1;
            padding: 24px;
            overflow-y: auto;
            min-width: 0;
        }
        
        .preview-pane {
            width: 320px;
            background: var(--bg-secondary);
            border-left: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            transition: width 0.3s;
        }
        
        .preview-pane.collapsed {
            width: 0;
            overflow: hidden;
        }
        
        .preview-header {
            padding: 16px 20px;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        
        .preview-header h3 {
            font-size: 14px;
            font-weight: 600;
        }
        
        .preview-content {
            flex: 1;
            overflow-y: auto;
            padding: 20px;
        }
        
        .preview-empty {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100%;
            color: var(--text-muted);
            text-align: center;
            padding: 20px;
        }
        
        .preview-empty .icon {
            font-size: 48px;
            margin-bottom: 16px;
            opacity: 0.5;
        }
        
        .qr-container {
            background: white;
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 20px;
            display: flex;
            justify-content: center;
        }
        
        .config-details {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        
        .detail-item {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        
        .detail-label {
            font-size: 11px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .detail-value {
            font-size: 13px;
            color: var(--text-primary);
            word-break: break-all;
        }
        
        .table-container {
            background: var(--bg-secondary);
            border-radius: 12px;
            border: 1px solid var(--border-color);
            overflow: hidden;
        }
        
        .table-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 16px 20px;
            border-bottom: 1px solid var(--border-color);
        }
        
        .table-header h2 {
            font-size: 16px;
            font-weight: 600;
        }
        
        .table-actions {
            display: flex;
            gap: 8px;
        }
        
        .data-table {
            width: 100%;
            border-collapse: collapse;
        }
        
        .data-table th {
            text-align: left;
            padding: 12px 16px;
            font-size: 11px;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            background: var(--bg-tertiary);
            cursor: pointer;
            user-select: none;
            white-space: nowrap;
        }
        
        .data-table th:hover {
            color: var(--text-primary);
        }
        
        .data-table th.sorted {
            color: var(--accent);
        }
        
        .data-table th .sort-icon {
            margin-left: 4px;
            opacity: 0.5;
        }
        
        .data-table th.sorted .sort-icon {
            opacity: 1;
        }
        
        .data-table td {
            padding: 12px 16px;
            font-size: 13px;
            border-bottom: 1px solid var(--border-color);
            max-width: 200px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .data-table tbody tr {
            cursor: pointer;
            transition: background 0.1s;
        }
        
        .data-table tbody tr:hover {
            background: var(--bg-hover);
        }
        
        .data-table tbody tr.selected {
            background: rgba(99, 102, 241, 0.15);
        }
        
        .data-table tbody tr.selected:hover {
            background: rgba(99, 102, 241, 0.2);
        }
        
        .checkbox-cell {
            width: 40px;
        }
        
        .checkbox {
            width: 16px;
            height: 16px;
            border-radius: 4px;
            border: 2px solid var(--border-color);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s;
        }
        
        .checkbox:hover {
            border-color: var(--accent);
        }
        
        .checkbox.checked {
            background: var(--accent);
            border-color: var(--accent);
        }
        
        .checkbox.checked::after {
            content: "✓";
            color: white;
            font-size: 10px;
            font-weight: bold;
        }
        
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 500;
        }
        
        .status-working {
            background: rgba(34, 197, 94, 0.15);
            color: var(--success);
        }
        
        .status-failed {
            background: rgba(239, 68, 68, 0.15);
            color: var(--danger);
        }
        
        .status-retired {
            background: rgba(245, 158, 11, 0.15);
            color: var(--warning);
        }
        
        .status-untested {
            background: rgba(107, 114, 128, 0.15);
            color: var(--text-muted);
        }
        
        .protocol-badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .protocol-vless { background: #3b82f6; color: white; }
        .protocol-vmess { background: #8b5cf6; color: white; }
        .protocol-trojan { background: #ec4899; color: white; }
        .protocol-ss, .protocol-shadowsocks { background: #14b8a6; color: white; }
        .protocol-hy2, .protocol-hysteria2 { background: #f59e0b; color: white; }
        .protocol-unknown { background: var(--bg-tertiary); color: var(--text-muted); }
        
        .context-menu {
            position: fixed;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 8px 0;
            min-width: 180px;
            box-shadow: 0 8px 30px rgba(0,0,0,0.4);
            z-index: 1000;
            display: none;
        }
        
        .context-menu.visible {
            display: block;
        }
        
        .context-menu-item {
            padding: 10px 16px;
            font-size: 13px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 10px;
            transition: background 0.1s;
        }
        
        .context-menu-item:hover {
            background: var(--bg-hover);
        }
        
        .context-menu-item.danger {
            color: var(--danger);
        }
        
        .context-menu-divider {
            height: 1px;
            background: var(--border-color);
            margin: 8px 0;
        }
        
        .modal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.7);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            backdrop-filter: blur(4px);
        }
        
        .modal-overlay.visible {
            display: flex;
        }
        
        .modal {
            background: var(--bg-secondary);
            border-radius: 16px;
            border: 1px solid var(--border-color);
            width: 90%;
            max-width: 500px;
            max-height: 90vh;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }
        
        .modal-header {
            padding: 20px 24px;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        
        .modal-header h3 {
            font-size: 18px;
            font-weight: 600;
        }
        
        .modal-close {
            width: 32px;
            height: 32px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            color: var(--text-muted);
            transition: all 0.2s;
        }
        
        .modal-close:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }
        
        .modal-body {
            padding: 24px;
            overflow-y: auto;
        }
        
        .modal-footer {
            padding: 16px 24px;
            border-top: 1px solid var(--border-color);
            display: flex;
            justify-content: flex-end;
            gap: 12px;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            font-size: 13px;
            font-weight: 500;
            margin-bottom: 8px;
            color: var(--text-secondary);
        }
        
        .form-group input,
        .form-group textarea,
        .form-group select {
            width: 100%;
            padding: 12px 16px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-primary);
            font-size: 14px;
            outline: none;
            transition: all 0.2s;
        }
        
        .form-group input:focus,
        .form-group textarea:focus,
        .form-group select:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px var(--accent-glow);
        }
        
        .form-group textarea {
            min-height: 120px;
            resize: vertical;
            font-family: monospace;
        }
        
        .toast-container {
            position: fixed;
            bottom: 24px;
            right: 24px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            z-index: 1001;
        }
        
        .toast {
            padding: 14px 20px;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            font-size: 13px;
            display: flex;
            align-items: center;
            gap: 10px;
            box-shadow: 0 8px 30px rgba(0,0,0,0.3);
            animation: slideIn 0.3s ease;
        }
        
        .toast.success { border-left: 3px solid var(--success); }
        .toast.error { border-left: 3px solid var(--danger); }
        .toast.info { border-left: 3px solid var(--info); }
        
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        
        .progress-bar {
            height: 4px;
            background: var(--bg-tertiary);
            border-radius: 2px;
            overflow: hidden;
            margin-top: 8px;
        }
        
        .progress-bar-fill {
            height: 100%;
            background: var(--accent);
            transition: width 0.3s;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        
        .stat-card {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
        }
        
        .stat-card .label {
            font-size: 12px;
            color: var(--text-muted);
            margin-bottom: 8px;
        }
        
        .stat-card .value {
            font-size: 28px;
            font-weight: 700;
        }
        
        .stat-card.success .value { color: var(--success); }
        .stat-card.warning .value { color: var(--warning); }
        .stat-card.danger .value { color: var(--danger); }
        .stat-card.info .value { color: var(--info); }
        
        .subscription-card {
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 16px;
        }
        
        .subscription-card:hover {
            border-color: var(--accent);
        }
        
        .subscription-info {
            flex: 1;
            min-width: 0;
        }
        
        .subscription-info h4 {
            font-size: 15px;
            font-weight: 600;
            margin-bottom: 4px;
        }
        
        .subscription-info .url {
            font-size: 12px;
            color: var(--text-muted);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .subscription-info .meta {
            font-size: 11px;
            color: var(--text-muted);
            margin-top: 8px;
        }
        
        .subscription-actions {
            display: flex;
            gap: 8px;
        }
        
        .settings-section {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            margin-bottom: 20px;
        }
        
        .settings-section-header {
            padding: 16px 20px;
            border-bottom: 1px solid var(--border-color);
        }
        
        .settings-section-header h3 {
            font-size: 15px;
            font-weight: 600;
        }
        
        .settings-section-body {
            padding: 20px;
        }
        
        .setting-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 0;
            border-bottom: 1px solid var(--border-color);
        }
        
        .setting-row:last-child {
            border-bottom: none;
        }
        
        .setting-info h4 {
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 4px;
        }
        
        .setting-info p {
            font-size: 12px;
            color: var(--text-muted);
        }
        
        .toggle {
            width: 44px;
            height: 24px;
            background: var(--bg-tertiary);
            border-radius: 12px;
            position: relative;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .toggle.active {
            background: var(--accent);
        }
        
        .toggle::after {
            content: "";
            position: absolute;
            width: 18px;
            height: 18px;
            background: white;
            border-radius: 50%;
            top: 3px;
            left: 3px;
            transition: all 0.2s;
        }
        
        .toggle.active::after {
            left: 23px;
        }
        
        .number-input {
            width: 80px;
            padding: 8px 12px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            color: var(--text-primary);
            font-size: 14px;
            text-align: center;
        }
        
        .empty-state {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 60px 20px;
            text-align: center;
        }
        
        .empty-state .icon {
            font-size: 64px;
            margin-bottom: 20px;
            opacity: 0.3;
        }
        
        .empty-state h3 {
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 8px;
        }
        
        .empty-state p {
            color: var(--text-muted);
            font-size: 14px;
            margin-bottom: 20px;
        }
        
        .selection-bar {
            position: fixed;
            bottom: 24px;
            left: 50%;
            transform: translateX(-50%);
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 12px 20px;
            display: none;
            align-items: center;
            gap: 16px;
            box-shadow: 0 8px 30px rgba(0,0,0,0.4);
            z-index: 100;
        }
        
        .selection-bar.visible {
            display: flex;
        }
        
        .selection-bar .count {
            font-size: 14px;
            font-weight: 500;
        }
        
        .selection-bar .actions {
            display: flex;
            gap: 8px;
        }
        
        .loading-spinner {
            width: 20px;
            height: 20px;
            border: 2px solid var(--border-color);
            border-top-color: var(--accent);
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        .tab-content {
            display: none;
        }
        
        .tab-content.active {
            display: block;
        }
        
        .copy-btn {
            padding: 4px 8px;
            font-size: 11px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: 4px;
            color: var(--text-secondary);
            cursor: pointer;
        }
        
        .copy-btn:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }
        
        .test-progress {
            display: none;
            align-items: center;
            gap: 12px;
            padding: 12px 16px;
            background: var(--bg-tertiary);
            border-radius: 8px;
            margin-bottom: 16px;
        }
        
        .test-progress.visible {
            display: flex;
        }
        
        .test-progress .info {
            flex: 1;
        }
        
        .test-progress .info .title {
            font-size: 13px;
            font-weight: 500;
        }
        
        .test-progress .info .subtitle {
            font-size: 11px;
            color: var(--text-muted);
        }
        
        ::-webkit-scrollbar {
            width: 10px;
            height: 10px;
        }
        
        ::-webkit-scrollbar-track {
            background: var(--bg-tertiary);
            border-radius: 5px;
        }
        
        ::-webkit-scrollbar-thumb {
            background: var(--border-color);
            border-radius: 5px;
            transition: background 0.2s;
        }
        
        ::-webkit-scrollbar-thumb:hover {
            background: var(--accent);
        }
        
        .testing-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 12px;
            padding: 4px;
        }
        
        #testing-content {
            max-height: calc(100vh - 250px);
            overflow-y: auto;
            overflow-x: hidden;
        }
        
        .test-card {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            transition: all 0.3s ease;
            position: relative;
            overflow: hidden;
        }
        
        .test-card.pending {
            opacity: 0.6;
        }
        
        .test-card.testing {
            border-color: var(--accent);
            box-shadow: 0 0 20px var(--accent-glow);
            animation: pulse 2s ease-in-out infinite;
        }
        
        .test-card.testing::before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(99, 102, 241, 0.2), transparent);
            animation: shimmer 1.5s infinite;
        }
        
        .test-card.success {
            border-color: var(--success);
            animation: successPop 0.5s ease-out;
        }
        
        .test-card.failed {
            border-color: var(--danger);
            animation: failShake 0.5s ease-out;
        }
        
        @keyframes pulse {
            0%, 100% { box-shadow: 0 0 20px var(--accent-glow); }
            50% { box-shadow: 0 0 30px rgba(99, 102, 241, 0.6); }
        }
        
        @keyframes shimmer {
            0% { left: -100%; }
            100% { left: 200%; }
        }
        
        @keyframes successPop {
            0% { transform: scale(1); }
            50% { transform: scale(1.05); }
            100% { transform: scale(1); }
        }
        
        @keyframes failShake {
            0%, 100% { transform: translateX(0); }
            25% { transform: translateX(-5px); }
            75% { transform: translateX(5px); }
        }
        
        .test-card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
        }
        
        .test-card-title {
            font-size: 14px;
            font-weight: 600;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            flex: 1;
        }
        
        .test-card-status {
            width: 24px;
            height: 24px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            flex-shrink: 0;
        }
        
        .test-card.pending .test-card-status {
            background: var(--bg-tertiary);
            color: var(--text-muted);
        }
        
        .test-card.testing .test-card-status {
            background: var(--accent);
            color: white;
            animation: spin 1s linear infinite;
        }
        
        .test-card.success .test-card-status {
            background: var(--success);
            color: white;
        }
        
        .test-card.failed .test-card-status,
        .test-card.error .test-card-status {
            background: var(--danger);
            color: white;
        }
        
        .test-card-info {
            display: flex;
            flex-direction: column;
            gap: 6px;
            font-size: 12px;
            color: var(--text-secondary);
        }
        
        .test-card-ping {
            font-size: 20px;
            font-weight: 700;
            color: var(--success);
        }
        
        .testing-stats {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            margin-bottom: 20px;
        }
        
        .testing-stat {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 12px;
            text-align: center;
        }
        
        .testing-stat-value {
            font-size: 24px;
            font-weight: 700;
            margin-bottom: 4px;
        }
        
        .testing-stat-label {
            font-size: 11px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .testing-stat.success .testing-stat-value { color: var(--success); }
        .testing-stat.failed .testing-stat-value { color: var(--danger); }
        .testing-stat.testing .testing-stat-value { color: var(--accent); }
        .testing-stat.pending .testing-stat-value { color: var(--text-muted); }
        
        .testing-empty {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 80px 20px;
            text-align: center;
        }
        
        .testing-empty .icon {
            font-size: 64px;
            margin-bottom: 20px;
            opacity: 0.3;
        }
        
        .testing-empty h3 {
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 8px;
        }
        
        .testing-empty p {
            color: var(--text-muted);
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="app-container">
        <div class="sidebar">
            <div class="logo">
                <h1>Subix Studio</h1>
                <span>V2Ray Subscription Suite</span>
            </div>
            <nav class="nav-menu">
                <div class="nav-item active" data-tab="subscriptions">
                    <div class="icon">📡</div>
                    <span>Subscriptions</span>
                    <span class="badge" id="sub-count">0</span>
                </div>
                <div class="nav-item" data-tab="all-configs">
                    <div class="icon">📋</div>
                    <span>All Configs</span>
                    <span class="badge" id="all-count">0</span>
                </div>
                <div class="nav-item" data-tab="working">
                    <div class="icon">✅</div>
                    <span>Working Configs</span>
                    <span class="badge" id="working-count">0</span>
                </div>
                <div class="nav-item" data-tab="retired">
                    <div class="icon">🗄️</div>
                    <span>Retired</span>
                    <span class="badge" id="retired-count">0</span>
                </div>
                <div class="nav-item" data-tab="testing">
                    <div class="icon">⚡</div>
                    <span>Testing</span>
                </div>
                <div class="nav-item" data-tab="preferences">
                    <div class="icon">⚙️</div>
                    <span>Preferences</span>
                </div>
            </nav>
        </div>
        
        <div class="main-content">
            <div class="header">
                <div class="search-box">
                    <input type="text" id="search-input" placeholder="Search configs...">
                </div>
                <div class="header-actions">
                    <button class="btn btn-secondary" id="refresh-btn">↻ Refresh</button>
                    <button class="btn btn-primary" id="test-all-btn">▶ Test All</button>
                </div>
            </div>
            
            <div class="content-wrapper">
                <div class="content-area">
                    <div class="test-progress" id="test-progress">
                        <div class="loading-spinner"></div>
                        <div class="info">
                            <div class="title">Testing configs...</div>
                            <div class="subtitle"><span id="progress-current">0</span> / <span id="progress-total">0</span></div>
                        </div>
                        <div class="progress-bar" style="flex:1">
                            <div class="progress-bar-fill" id="progress-bar" style="width:0%"></div>
                        </div>
                    </div>
                    
                    <div class="tab-content active" id="tab-subscriptions">
                        <div class="stats-grid">
                            <div class="stat-card">
                                <div class="label">Total Subscriptions</div>
                                <div class="value" id="stat-subs">0</div>
                            </div>
                            <div class="stat-card info">
                                <div class="label">Total Configs</div>
                                <div class="value" id="stat-total">0</div>
                            </div>
                        </div>
                        
                        <div class="table-container">
                            <div class="table-header">
                                <h2>Subscriptions</h2>
                                <div class="table-actions">
                                    <button class="btn btn-primary" onclick="showAddSubscriptionModal()">+ Add Subscription</button>
                                </div>
                            </div>
                            <div id="subscriptions-list"></div>
                        </div>
                    </div>
                    
                    <div class="tab-content" id="tab-all-configs">
                        <div class="stats-grid">
                            <div class="stat-card info">
                                <div class="label">Total</div>
                                <div class="value" id="stat-all-total">0</div>
                            </div>
                            <div class="stat-card success">
                                <div class="label">Working</div>
                                <div class="value" id="stat-all-working">0</div>
                            </div>
                            <div class="stat-card danger">
                                <div class="label">Failed</div>
                                <div class="value" id="stat-all-failed">0</div>
                            </div>
                            <div class="stat-card warning">
                                <div class="label">Untested</div>
                                <div class="value" id="stat-all-untested">0</div>
                            </div>
                        </div>
                        <div class="table-container">
                            <div class="table-header">
                                <h2>All Configurations</h2>
                                <div class="table-actions">
                                    <button class="btn btn-secondary" onclick="showAddSingleConfigModal()">+ Add Single</button>
                                    <button class="btn btn-secondary" onclick="showAddConfigsModal()">+ Add Bulk</button>
                                </div>
                            </div>
                            <div class="table-wrapper">
                                <table class="data-table" id="configs-table">
                                    <thead>
                                        <tr>
                                            <th class="checkbox-cell"><div class="checkbox" id="select-all-configs"></div></th>
                                            <th data-sort="display_tag">Name <span class="sort-icon">↕</span></th>
                                            <th data-sort="protocol">Protocol <span class="sort-icon">↕</span></th>
                                            <th data-sort="address">Address <span class="sort-icon">↕</span></th>
                                            <th data-sort="status">Status <span class="sort-icon">↕</span></th>
                                            <th data-sort="last_ping_ms">Ping <span class="sort-icon">↕</span></th>
                                            <th data-sort="last_tested">Last Test <span class="sort-icon">↕</span></th>
                                        </tr>
                                    </thead>
                                    <tbody id="configs-body"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                    
                    <div class="tab-content" id="tab-working">
                        <div class="table-container">
                            <div class="table-header">
                                <h2>Working Configurations</h2>
                                <div class="table-actions">
                                    <button class="btn btn-danger" onclick="retireAllWorking()">Retire All</button>
                                </div>
                            </div>
                            <div class="table-wrapper">
                                <table class="data-table">
                                    <thead>
                                        <tr>
                                            <th class="checkbox-cell"><div class="checkbox" id="select-all-working"></div></th>
                                            <th data-sort="display_tag">Name <span class="sort-icon">↕</span></th>
                                            <th data-sort="protocol">Protocol <span class="sort-icon">↕</span></th>
                                            <th data-sort="address">Address <span class="sort-icon">↕</span></th>
                                            <th data-sort="last_ping_ms">Ping <span class="sort-icon">↕</span></th>
                                            <th data-sort="successful_tests">Success Rate <span class="sort-icon">↕</span></th>
                                            <th data-sort="last_tested">Last Test <span class="sort-icon">↕</span></th>
                                        </tr>
                                    </thead>
                                    <tbody id="working-body"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                    
                    <div class="tab-content" id="tab-retired">
                        <div class="table-container">
                            <div class="table-header">
                                <h2>Retired Configurations</h2>
                                <div class="table-actions">
                                    <button class="btn btn-secondary" onclick="restoreSelected()">↩ Restore Selected</button>
                                </div>
                            </div>
                            <div class="table-wrapper">
                                <table class="data-table">
                                    <thead>
                                        <tr>
                                            <th class="checkbox-cell"><div class="checkbox" id="select-all-retired"></div></th>
                                            <th data-sort="display_tag">Name <span class="sort-icon">↕</span></th>
                                            <th data-sort="protocol">Protocol <span class="sort-icon">↕</span></th>
                                            <th data-sort="address">Address <span class="sort-icon">↕</span></th>
                                            <th data-sort="consecutive_failures">Failures <span class="sort-icon">↕</span></th>
                                            <th data-sort="last_tested">Retired On <span class="sort-icon">↕</span></th>
                                        </tr>
                                    </thead>
                                    <tbody id="retired-body"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                    
                    <div class="tab-content" id="tab-testing">
                        <div class="testing-stats" id="testing-stats">
                            <div class="testing-stat pending">
                                <div class="testing-stat-value" id="test-stat-pending">0</div>
                                <div class="testing-stat-label">Pending</div>
                            </div>
                            <div class="testing-stat testing">
                                <div class="testing-stat-value" id="test-stat-testing">0</div>
                                <div class="testing-stat-label">Testing</div>
                            </div>
                            <div class="testing-stat success">
                                <div class="testing-stat-value" id="test-stat-success">0</div>
                                <div class="testing-stat-label">Success</div>
                            </div>
                            <div class="testing-stat failed">
                                <div class="testing-stat-value" id="test-stat-failed">0</div>
                                <div class="testing-stat-label">Failed</div>
                            </div>
                        </div>
                        
                        <div id="testing-content">
                            <div class="testing-empty">
                                <div class="icon">⚡</div>
                                <h3>No Active Tests</h3>
                                <p>Start a test to see live progress here</p>
                            </div>
                        </div>
                    </div>
                    
                    <div class="tab-content" id="tab-preferences">
                        <div class="settings-section">
                            <div class="settings-section-header">
                                <h3>Auto Mode</h3>
                            </div>
                            <div class="settings-section-body">
                                <div class="setting-row">
                                    <div class="setting-info">
                                        <h4>Enable Auto Mode</h4>
                                        <p>Automatically tune all settings for optimal performance</p>
                                    </div>
                                    <div class="toggle" id="toggle-auto-mode" onclick="toggleSetting('auto_mode')"></div>
                                </div>
                            </div>
                        </div>
                        
                        <div class="settings-section">
                            <div class="settings-section-header">
                                <h3>Engine Mode</h3>
                            </div>
                            <div class="settings-section-body">
                                <div class="setting-row">
                                    <div class="setting-info">
                                        <h4>Enable Engine Mode</h4>
                                        <p>Continuously test configs in the background (overrides auto mode)</p>
                                    </div>
                                    <div class="toggle" id="toggle-engine-mode" onclick="toggleSetting('engine_mode')"></div>
                                </div>
                            </div>
                        </div>
                        
                        <div class="settings-section" id="manual-settings">
                            <div class="settings-section-header">
                                <h3>Testing Settings</h3>
                            </div>
                            <div class="settings-section-body">
                                <div class="setting-row">
                                    <div class="setting-info">
                                        <h4>Test Interval (minutes)</h4>
                                        <p>How often to re-test working configs (Auto Mode only)</p>
                                    </div>
                                    <input type="number" class="number-input" id="setting-interval" value="30" min="5" max="1440">
                                </div>
                                <div class="setting-row">
                                    <div class="setting-info">
                                        <h4>Concurrent Tests</h4>
                                        <p>Maximum parallel test connections</p>
                                    </div>
                                    <input type="number" class="number-input" id="setting-concurrent" value="10" min="1" max="50">
                                </div>
                                <div class="setting-row">
                                    <div class="setting-info">
                                        <h4>Test Timeout (seconds)</h4>
                                        <p>Maximum time to wait for each test</p>
                                    </div>
                                    <input type="number" class="number-input" id="setting-timeout" value="15" min="5" max="120">
                                </div>
                                <div class="setting-row">
                                    <div class="setting-info">
                                        <h4>Failure Threshold</h4>
                                        <p>Consecutive failures before retirement</p>
                                    </div>
                                    <input type="number" class="number-input" id="setting-threshold" value="3" min="1" max="10">
                                </div>
                            </div>
                        </div>
                        
                        <div class="settings-section" id="manual-settings-2">
                            <div class="settings-section-header">
                                <h3>Config Management</h3>
                            </div>
                            <div class="settings-section-body">
                                <div class="setting-row">
                                    <div class="setting-info">
                                        <h4>Auto Deduplicate</h4>
                                        <p>Remove duplicate configs automatically</p>
                                    </div>
                                    <div class="toggle" id="toggle-dedup" onclick="toggleSetting('auto_deduplicate')"></div>
                                </div>
                                <div class="setting-row">
                                    <div class="setting-info">
                                        <h4>Auto Test New Configs</h4>
                                        <p>Test configs immediately when added</p>
                                    </div>
                                    <div class="toggle" id="toggle-autotest" onclick="toggleSetting('auto_test_new_configs')"></div>
                                </div>
                            </div>
                        </div>
                        
                        <button class="btn btn-primary" onclick="saveSettings()">Save Settings</button>
                    </div>
                </div>
                
                <div class="preview-pane" id="preview-pane">
                    <div class="preview-header">
                        <h3>Config Details</h3>
                        <div class="modal-close" onclick="closePreview()">✕</div>
                    </div>
                    <div class="preview-content">
                        <div class="preview-empty" id="preview-empty">
                            <div class="icon">📄</div>
                            <p>Select a config to view details</p>
                        </div>
                        <div id="preview-details" style="display:none">
                            <div class="qr-container" id="qr-container"></div>
                            <div class="config-details" id="config-details"></div>
                            <div style="margin-top:20px">
                                <button class="btn btn-secondary" style="width:100%" onclick="copyConfigUri()">
                                    📋 Copy URI
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="context-menu" id="context-menu">
        <div class="context-menu-item" onclick="contextAction('test')">▶ Test Selected</div>
        <div class="context-menu-item" onclick="contextAction('copy')">📋 Copy URIs</div>
        <div class="context-menu-item" onclick="contextAction('export')">💾 Export Selected</div>
        <div class="context-menu-divider"></div>
        <div class="context-menu-item" onclick="contextAction('restore')" id="ctx-restore" style="display:none">↩ Restore</div>
        <div class="context-menu-item danger" onclick="contextAction('delete')">🗑 Delete</div>
    </div>
    
    <div class="selection-bar" id="selection-bar">
        <span class="count"><span id="selected-count">0</span> selected</span>
        <div class="actions">
            <button class="btn btn-secondary" onclick="testSelected()">▶ Test</button>
            <button class="btn btn-secondary" onclick="exportSelected()">💾 Export</button>
            <button class="btn btn-danger" onclick="deleteSelected()">🗑 Delete</button>
        </div>
    </div>
    
    <div class="modal-overlay" id="modal-overlay">
        <div class="modal" id="modal">
            <div class="modal-header">
                <h3 id="modal-title">Modal</h3>
                <div class="modal-close" onclick="closeModal()">✕</div>
            </div>
            <div class="modal-body" id="modal-body"></div>
            <div class="modal-footer" id="modal-footer"></div>
        </div>
    </div>
    
    <div class="toast-container" id="toast-container"></div>
    
    <script>
        const state = {
            currentTab: 'subscriptions',
            configs: [],
            subscriptions: [],
            selectedConfigs: new Set(),
            previewConfig: null,
            sortBy: 'id',
            sortOrder: 'desc',
            searchQuery: '',
            isPolling: false,
            settings: {}
        };
        
        async function api(endpoint, method = 'GET', data = null) {
            const options = { method, headers: { 'Content-Type': 'application/json' } };
            if (data) options.body = JSON.stringify(data);
            const response = await fetch('/api/' + endpoint, options);
            return response.json();
        }
        
        function showToast(message, type = 'info') {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = 'toast ' + type;
            toast.textContent = message;
            container.appendChild(toast);
            setTimeout(() => toast.remove(), 4000);
        }
        
        function switchTab(tabId) {
            state.currentTab = tabId;
            document.querySelectorAll('.nav-item').forEach(item => {
                item.classList.toggle('active', item.dataset.tab === tabId);
            });
            document.querySelectorAll('.tab-content').forEach(content => {
                content.classList.toggle('active', content.id === 'tab-' + tabId);
            });
            state.selectedConfigs.clear();
            document.querySelectorAll('.table-header .checkbox').forEach(cb => cb.classList.remove('checked'));
            updateSelectionBar();
            loadData();
        }
        
        async function loadData() {
            const stats = await api('stats');
            document.getElementById('sub-count').textContent = stats.subscriptions || 0;
            document.getElementById('all-count').textContent = stats.total || 0;
            document.getElementById('working-count').textContent = stats.working || 0;
            document.getElementById('retired-count').textContent = stats.retired || 0;
            
            if (state.currentTab === 'subscriptions') {
                await loadSubscriptions();
                document.getElementById('stat-subs').textContent = stats.subscriptions || 0;
                document.getElementById('stat-total').textContent = stats.total || 0;
            } else if (state.currentTab === 'all-configs') {
                await loadConfigs('');
                document.getElementById('stat-all-total').textContent = stats.total || 0;
                document.getElementById('stat-all-working').textContent = stats.working || 0;
                document.getElementById('stat-all-failed').textContent = stats.failed || 0;
                document.getElementById('stat-all-untested').textContent = stats.untested || 0;
            } else if (state.currentTab === 'working') {
                await loadConfigs('working');
            } else if (state.currentTab === 'retired') {
                await loadConfigs('retired');
            } else if (state.currentTab === 'testing') {
                startTestingMonitor();
            } else if (state.currentTab === 'preferences') {
                await loadSettings();
            }
            
            // Check global test status to update button
            const progress = await api('test/progress');
            if (progress.status === 'testing') {
                updateTestButton(true);
                if (!state.isPolling) pollTestProgress();
            } else {
                updateTestButton(false);
            }
        }
        
        async function loadSubscriptions() {
            const subs = await api('subscriptions');
            state.subscriptions = subs;
            const container = document.getElementById('subscriptions-list');
            
            if (!subs.length) {
                container.innerHTML = '<div class="empty-state"><div class="icon">📡</div><h3>No subscriptions yet</h3><p>Add a subscription to get started</p><button class="btn btn-primary" onclick="showAddSubscriptionModal()">+ Add Subscription</button></div>';
                return;
            }
            
            container.innerHTML = subs.map(sub => `
                <div class="subscription-card" data-id="${sub.id}">
                    <div class="subscription-info">
                        <h4>${escapeHtml(sub.name)}</h4>
                        <div class="url">${escapeHtml(sub.url)}</div>
                        <div class="meta">${sub.config_count || 0} configs • Last updated: ${sub.last_updated ? formatDate(sub.last_updated) : 'Never'}</div>
                    </div>
                    <div class="subscription-actions">
                        <button class="btn btn-secondary" onclick="updateSubscription(${sub.id})">↻ Update</button>
                        <button class="btn btn-danger" onclick="deleteSubscription(${sub.id})">🗑</button>
                    </div>
                </div>
            `).join('');
        }
        
        async function loadConfigs(statusFilter) {
            const params = new URLSearchParams({
                status: statusFilter,
                search: state.searchQuery,
                sort_by: state.sortBy,
                sort_order: state.sortOrder
            });
            
            const configs = await api('configs?' + params);
            state.configs = configs;
            
            let tbody;
            if (statusFilter === 'working') {
                tbody = document.getElementById('working-body');
            } else if (statusFilter === 'retired') {
                tbody = document.getElementById('retired-body');
            } else {
                tbody = document.getElementById('configs-body');
            }
            
            if (!configs.length) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--text-muted)">No configs found</td></tr>';
                return;
            }
            
            tbody.innerHTML = configs.map(config => {
                const isSelected = state.selectedConfigs.has(config.id);
                const successRate = config.total_tests > 0 ? Math.round((config.successful_tests / config.total_tests) * 100) : 0;
                
                return `
                    <tr class="${isSelected ? 'selected' : ''}" data-id="${config.id}" onclick="selectConfig(event, ${config.id})" oncontextmenu="showContextMenu(event, ${config.id})">
                        <td class="checkbox-cell"><div class="checkbox ${isSelected ? 'checked' : ''}" onclick="toggleConfigSelection(event, ${config.id})"></div></td>
                        <td title="${escapeHtml(config.display_tag || 'Unnamed')}">${escapeHtml(config.display_tag || 'Unnamed')}</td>
                        <td><span class="protocol-badge protocol-${(config.protocol || 'unknown').toLowerCase()}">${config.protocol || '?'}</span></td>
                        <td title="${escapeHtml(config.address || '')}">${escapeHtml(config.address || '-')}:${config.port || '-'}</td>
                        ${statusFilter === 'retired' ? `
                            <td>${config.consecutive_failures || 0}</td>
                            <td>${config.last_tested ? formatDate(config.last_tested) : '-'}</td>
                        ` : statusFilter === 'working' ? `
                            <td>${config.last_ping_ms ? config.last_ping_ms + ' ms' : '-'}</td>
                            <td>${successRate}%</td>
                            <td>${config.last_tested ? formatDate(config.last_tested) : '-'}</td>
                        ` : `
                            <td><span class="status-badge status-${config.status || 'untested'}">${config.status || 'untested'}</span></td>
                            <td>${config.last_ping_ms ? config.last_ping_ms + ' ms' : '-'}</td>
                            <td>${config.last_tested ? formatDate(config.last_tested) : '-'}</td>
                        `}
                    </tr>
                `;
            }).join('');
        }
        
        async function loadSettings() {
            const settings = await api('settings');
            state.settings = settings;
            
            document.getElementById('toggle-auto-mode').classList.toggle('active', settings.auto_mode);
            document.getElementById('toggle-engine-mode').classList.toggle('active', settings.engine_mode);
            document.getElementById('toggle-dedup').classList.toggle('active', settings.auto_deduplicate);
            document.getElementById('toggle-autotest').classList.toggle('active', settings.auto_test_new_configs);
            
            document.getElementById('setting-interval').value = settings.test_interval_minutes || 30;
            document.getElementById('setting-concurrent').value = settings.max_concurrent_tests || 10;
            document.getElementById('setting-timeout').value = settings.test_timeout_seconds || 15;
            document.getElementById('setting-threshold').value = settings.failure_threshold || 3;
            
            const disabled = settings.auto_mode || settings.engine_mode;
            document.getElementById('manual-settings').style.opacity = disabled ? 0.5 : 1;
            document.getElementById('manual-settings-2').style.opacity = disabled ? 0.5 : 1;
            document.querySelectorAll('#manual-settings input, #manual-settings-2 .toggle').forEach(el => {
                el.style.pointerEvents = disabled ? 'none' : 'auto';
            });
        }
        
        function toggleSetting(key) {
            if (key === 'engine_mode' && state.settings.engine_mode) {
                state.settings.engine_mode = false;
            } else if (key === 'engine_mode') {
                state.settings.engine_mode = true;
                state.settings.auto_mode = false;
            } else if (key === 'auto_mode' && state.settings.auto_mode) {
                state.settings.auto_mode = false;
            } else if (key === 'auto_mode') {
                state.settings.auto_mode = true;
                state.settings.engine_mode = false;
            } else if ((state.settings.auto_mode || state.settings.engine_mode) && key !== 'auto_mode' && key !== 'engine_mode') {
                return;
            } else {
                state.settings[key] = !state.settings[key];
            }
            loadSettings();
        }
        
        async function saveSettings() {
            state.settings.test_interval_minutes = parseInt(document.getElementById('setting-interval').value);
            state.settings.max_concurrent_tests = parseInt(document.getElementById('setting-concurrent').value);
            state.settings.test_timeout_seconds = parseInt(document.getElementById('setting-timeout').value);
            state.settings.failure_threshold = parseInt(document.getElementById('setting-threshold').value);
            
            await api('settings', 'POST', state.settings);
            showToast('Settings saved', 'success');
        }
        
        function selectConfig(event, configId) {
            if (event.target.classList.contains('checkbox')) return;
            
            const config = state.configs.find(c => c.id === configId);
            if (!config) return;
            
            if (event.ctrlKey || event.metaKey) {
                toggleConfigSelection(event, configId);
            } else if (event.shiftKey && state.previewConfig) {
                const startIdx = state.configs.findIndex(c => c.id === state.previewConfig.id);
                const endIdx = state.configs.findIndex(c => c.id === configId);
                const [from, to] = startIdx < endIdx ? [startIdx, endIdx] : [endIdx, startIdx];
                for (let i = from; i <= to; i++) {
                    state.selectedConfigs.add(state.configs[i].id);
                }
                updateSelectionBar();
                loadData();
            } else {
                showPreview(config);
            }
        }
        
        function toggleConfigSelection(event, configId) {
            event.stopPropagation();
            if (state.selectedConfigs.has(configId)) {
                state.selectedConfigs.delete(configId);
            } else {
                state.selectedConfigs.add(configId);
            }
            updateSelectionBar();
            
            const row = document.querySelector(`tr[data-id="${configId}"]`);
            if (row) {
                row.classList.toggle('selected', state.selectedConfigs.has(configId));
                row.querySelector('.checkbox').classList.toggle('checked', state.selectedConfigs.has(configId));
            }
        }
        
        function updateSelectionBar() {
            const bar = document.getElementById('selection-bar');
            const count = state.selectedConfigs.size;
            document.getElementById('selected-count').textContent = count;
            bar.classList.toggle('visible', count > 0);
        }
        
        async function showPreview(config) {
            state.previewConfig = config;
            document.getElementById('preview-empty').style.display = 'none';
            document.getElementById('preview-details').style.display = 'block';
            
            const qr = await api('qrcode?uri=' + encodeURIComponent(config.uri));
            document.getElementById('qr-container').innerHTML = qr.svg;
            
            const details = document.getElementById('config-details');
            details.innerHTML = `
                <div class="detail-item">
                    <span class="detail-label">Name</span>
                    <span class="detail-value">${escapeHtml(config.display_tag || 'Unnamed')}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Protocol</span>
                    <span class="detail-value"><span class="protocol-badge protocol-${(config.protocol || 'unknown').toLowerCase()}">${config.protocol || 'Unknown'}</span></span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Address</span>
                    <span class="detail-value">${escapeHtml(config.address || '-')}:${config.port || '-'}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Status</span>
                    <span class="detail-value"><span class="status-badge status-${config.status || 'untested'}">${config.status || 'untested'}</span></span>
                </div>
                ${config.last_ping_ms ? `
                <div class="detail-item">
                    <span class="detail-label">Last Ping</span>
                    <span class="detail-value">${config.last_ping_ms} ms</span>
                </div>` : ''}
                ${config.security ? `
                <div class="detail-item">
                    <span class="detail-label">Security</span>
                    <span class="detail-value">${escapeHtml(config.security)}</span>
                </div>` : ''}
                ${config.network ? `
                <div class="detail-item">
                    <span class="detail-label">Network</span>
                    <span class="detail-value">${escapeHtml(config.network)}</span>
                </div>` : ''}
                <div class="detail-item">
                    <span class="detail-label">Tests</span>
                    <span class="detail-value">${config.successful_tests || 0} / ${config.total_tests || 0} successful</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">URI</span>
                    <span class="detail-value" style="font-family:monospace;font-size:11px;word-break:break-all">${escapeHtml(config.uri)}</span>
                </div>
            `;
        }
        
        function closePreview() {
            state.previewConfig = null;
            document.getElementById('preview-empty').style.display = 'flex';
            document.getElementById('preview-details').style.display = 'none';
        }
        
        async function copyConfigUri() {
            if (!state.previewConfig) return;
            await navigator.clipboard.writeText(state.previewConfig.uri);
            showToast('URI copied to clipboard', 'success');
        }
        
        function showContextMenu(event, configId) {
            event.preventDefault();
            
            if (!state.selectedConfigs.has(configId)) {
                state.selectedConfigs.clear();
                state.selectedConfigs.add(configId);
                updateSelectionBar();
                loadData();
            }
            
            const menu = document.getElementById('context-menu');
            menu.style.left = event.pageX + 'px';
            menu.style.top = event.pageY + 'px';
            menu.classList.add('visible');
            
            document.getElementById('ctx-restore').style.display = 
                state.currentTab === 'retired' ? 'block' : 'none';
        }
        
        function hideContextMenu() {
            document.getElementById('context-menu').classList.remove('visible');
        }
        
        async function contextAction(action) {
            hideContextMenu();
            const ids = Array.from(state.selectedConfigs);
            
            if (action === 'test') {
                await testSelected();
            } else if (action === 'copy') {
                const configs = state.configs.filter(c => ids.includes(c.id));
                const uris = configs.map(c => c.uri).join('\\n');
                await navigator.clipboard.writeText(uris);
                showToast(`${configs.length} URIs copied`, 'success');
            } else if (action === 'export') {
                await exportSelected();
            } else if (action === 'restore') {
                await restoreSelected();
            } else if (action === 'delete') {
                await deleteSelected();
            }
        }
        
        async function testSelected() {
            const ids = Array.from(state.selectedConfigs);
            if (!ids.length) {
                showToast('No configs selected', 'error');
                return;
            }
            
            const result = await api('test', 'POST', { config_ids: ids });
            if (result.success) {
                showToast('Testing started', 'info');
                switchTab('testing');
                pollTestProgress();
            } else {
                showToast(result.error || 'Failed to start test', 'error');
            }
        }
        
        async function exportSelected() {
            const ids = Array.from(state.selectedConfigs);
            if (!ids.length) {
                showToast('No configs selected', 'error');
                return;
            }
            
            const configs = state.configs.filter(c => ids.includes(c.id));
            const uris = configs.map(c => c.uri).join('\\n');
            
            const blob = new Blob([uris], { type: 'text/plain' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'configs_export.txt';
            a.click();
            URL.revokeObjectURL(url);
            
            showToast(`${configs.length} configs exported`, 'success');
        }
        
        async function deleteSelected() {
            const ids = Array.from(state.selectedConfigs);
            if (!ids.length) return;
            
            if (!confirm(`Delete ${ids.length} config(s)?`)) return;
            
            await api('configs', 'DELETE', { ids });
            state.selectedConfigs.clear();
            updateSelectionBar();
            loadData();
            showToast('Configs deleted', 'success');
        }
        
        async function restoreSelected() {
            const ids = Array.from(state.selectedConfigs);
            if (!ids.length) return;
            
            await api('configs/restore', 'POST', { ids });
            state.selectedConfigs.clear();
            updateSelectionBar();
            loadData();
            showToast('Configs restored', 'success');
        }
        
        async function retireAllWorking() {
            if (!confirm('Are you sure you want to retire ALL working configs?')) return;
            
            await api('configs/retire_working', 'POST');
            showToast('All working configs retired', 'success');
            loadData();
        }
        
        function toggleSelectAll(elementId) {
            const el = document.getElementById(elementId);
            const isChecked = el.classList.toggle('checked');
            
            state.configs.forEach(c => {
                if (isChecked) {
                    state.selectedConfigs.add(c.id);
                } else {
                    state.selectedConfigs.delete(c.id);
                }
                
                const row = document.querySelector(`tr[data-id="${c.id}"]`);
                if (row) {
                    row.classList.toggle('selected', isChecked);
                    const cb = row.querySelector('.checkbox');
                    if (cb) cb.classList.toggle('checked', isChecked);
                }
            });
            
            updateSelectionBar();
        }
        
        function showModal(title, body, footer) {
            document.getElementById('modal-title').textContent = title;
            document.getElementById('modal-body').innerHTML = body;
            document.getElementById('modal-footer').innerHTML = footer;
            document.getElementById('modal-overlay').classList.add('visible');
        }
        
        function closeModal() {
            document.getElementById('modal-overlay').classList.remove('visible');
        }
        
        function showAddSubscriptionModal() {
            showModal('Add Subscription', `
                <div class="form-group">
                    <label>Name</label>
                    <input type="text" id="sub-name" placeholder="My Subscription">
                </div>
                <div class="form-group">
                    <label>URL</label>
                    <input type="text" id="sub-url" placeholder="https://example.com/subscribe">
                </div>
            `, `
                <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                <button class="btn btn-primary" onclick="addSubscription()">Add</button>
            `);
        }
        
        async function addSubscription() {
            const name = document.getElementById('sub-name').value.trim();
            const url = document.getElementById('sub-url').value.trim();
            
            if (!name || !url) {
                showToast('Please fill all fields', 'error');
                return;
            }
            
            const result = await api('subscriptions', 'POST', { name, url });
            if (result.id) {
                closeModal();
                showToast('Subscription added', 'success');
                loadData();
                
                showToast('Updating subscription...', 'info');
                await updateSubscription(result.id);
            } else {
                showToast(result.error || 'Failed to add subscription', 'error');
            }
        }
        
        async function updateSubscription(subId) {
            const result = await api(`subscriptions/${subId}/update`, 'POST');
            if (result.success) {
                showToast(`Updated: ${result.added} new configs`, 'success');
                loadData();
            } else {
                showToast(result.error || 'Failed to update', 'error');
            }
        }
        
        async function deleteSubscription(subId) {
            if (!confirm('Delete this subscription?')) return;
            
            await api(`subscriptions/${subId}`, 'DELETE');
            showToast('Subscription deleted', 'success');
            loadData();
        }
        
        function showAddSingleConfigModal() {
            showModal('Add Single Config', `
                <div class="form-group">
                    <label>Config URI</label>
                    <textarea id="single-config-uri" placeholder="vless://..." style="min-height:80px"></textarea>
                </div>
            `, `
                <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                <button class="btn btn-primary" onclick="addSingleConfig()">Add</button>
            `);
        }
        
        function showAddConfigsModal() {
            showModal('Add Configs in Bulk', `
                <div class="form-group">
                    <label>Paste URIs (one per line)</label>
                    <textarea id="config-uris" placeholder="vless://...&#10;vmess://...&#10;trojan://..."></textarea>
                </div>
            `, `
                <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                <button class="btn btn-primary" onclick="addConfigs()">Add</button>
            `);
        }
        
        async function addSingleConfig() {
            const uri = document.getElementById('single-config-uri').value.trim();
            
            if (!uri || !uri.includes('://')) {
                showToast('Invalid URI', 'error');
                return;
            }
            
            const result = await api('configs', 'POST', { uris: [uri] });
            closeModal();
            if (result.added > 0) {
                showToast('Config added successfully', 'success');
            } else {
                showToast('Config already exists or invalid', 'error');
            }
            loadData();
        }
        
        async function addConfigs() {
            const uris = document.getElementById('config-uris').value
                .split('\\n')
                .map(u => u.trim())
                .filter(u => u && u.includes('://'));
            
            if (!uris.length) {
                showToast('No valid URIs found', 'error');
                return;
            }
            
            const result = await api('configs', 'POST', { uris });
            closeModal();
            showToast(`Added ${result.added || 0} configs`, 'success');
            loadData();
        }
        
        let testingMonitorInterval = null;
        
        function startTestingMonitor() {
            if (testingMonitorInterval) clearInterval(testingMonitorInterval);
            updateTestingView();
            testingMonitorInterval = setInterval(updateTestingView, 500);
        }
        
        function stopTestingMonitor() {
            if (testingMonitorInterval) {
                clearInterval(testingMonitorInterval);
                testingMonitorInterval = null;
            }
        }
        
        async function updateTestingView() {
            if (state.currentTab !== 'testing') {
                stopTestingMonitor();
                return;
            }
            
            const progress = await api('test/progress');
            
            const pending = progress.configs?.filter(c => c.status === 'pending').length || 0;
            const testing = progress.configs?.filter(c => c.status === 'testing').length || 0;
            const success = progress.configs?.filter(c => c.result === 'success').length || 0;
            const failed = progress.configs?.filter(c => c.result === 'failed' || c.result === 'error').length || 0;
            
            document.getElementById('test-stat-pending').textContent = pending;
            document.getElementById('test-stat-testing').textContent = testing;
            document.getElementById('test-stat-success').textContent = success;
            document.getElementById('test-stat-failed').textContent = failed;
            
            const content = document.getElementById('testing-content');
            
            if (!progress.configs || progress.configs.length === 0) {
                content.innerHTML = '<div class="testing-empty"><div class="icon">⚡</div><h3>No Active Tests</h3><p>Start a test to see live progress here</p></div>';
                return;
            }
            
            const scrollTop = content.scrollTop;
            
            let html = '<div class="testing-grid">';
            for (const config of progress.configs) {
                const statusClass = config.result ? config.result : config.status;
                const statusIcon = 
                    config.result === 'success' ? '✓' :
                    config.result === 'failed' || config.result === 'error' ? '✕' :
                    config.status === 'testing' ? '⚡' : '○';
                
                html += `
                    <div class="test-card ${statusClass}">
                        <div class="test-card-header">
                            <div class="test-card-title" title="${escapeHtml(config.name)}">${escapeHtml(config.name)}</div>
                            <div class="test-card-status">${statusIcon}</div>
                        </div>
                        <div class="test-card-info">
                            <div><span class="protocol-badge protocol-${(config.protocol || 'unknown').toLowerCase()}">${config.protocol || 'unknown'}</span></div>
                            ${config.ping ? `<div class="test-card-ping">${config.ping}ms</div>` : ''}
                        </div>
                    </div>
                `;
            }
            html += '</div>';
            
            content.innerHTML = html;
            content.scrollTop = scrollTop;
        }
        
        function updateTestButton(isRunning) {
            const btn = document.getElementById('test-all-btn');
            if (isRunning) {
                btn.innerHTML = '■ Stop Test';
                btn.classList.remove('btn-primary');
                btn.classList.add('btn-danger');
            } else {
                btn.innerHTML = '▶ Test All';
                btn.classList.remove('btn-danger');
                btn.classList.add('btn-primary');
            }
        }

        async function toggleTesting() {
            const btn = document.getElementById('test-all-btn');
            const isRunning = btn.classList.contains('btn-danger');

            if (isRunning) {
                await api('test/stop', 'POST');
                showToast('Stopping tests...', 'info');
                updateTestButton(false);
            } else {
                const result = await api('test', 'POST', {});
                if (result.success) {
                    showToast('Testing started', 'info');
                    switchTab('testing');
                    updateTestButton(true);
                    pollTestProgress();
                } else {
                    showToast(result.error || 'Failed to start test', 'error');
                }
            }
        }
        
        async function pollTestProgress() {
            if (state.isPolling) return;
            state.isPolling = true;
            
            const progressEl = document.getElementById('test-progress');
            progressEl.classList.add('visible');
            
            const poll = async () => {
                const progress = await api('test/progress');
                
                document.getElementById('progress-current').textContent = progress.current;
                document.getElementById('progress-total').textContent = progress.total;
                
                const pct = progress.total > 0 ? (progress.current / progress.total) * 100 : 0;
                document.getElementById('progress-bar').style.width = pct + '%';
                
                if (progress.status === 'testing' && progress.current < progress.total) {
                    setTimeout(poll, 1000);
                } else {
                    state.isPolling = false;
                    progressEl.classList.remove('visible');
                    updateTestButton(false);
                    loadData();
                    if (progress.status === 'completed') {
                        showToast('Testing completed', 'success');
                    }
                }
            };
            
            poll();
        }
        
        function setupSorting() {
            document.querySelectorAll('.data-table th[data-sort]').forEach(th => {
                th.addEventListener('click', () => {
                    const sortKey = th.dataset.sort;
                    if (state.sortBy === sortKey) {
                        state.sortOrder = state.sortOrder === 'asc' ? 'desc' : 'asc';
                    } else {
                        state.sortBy = sortKey;
                        state.sortOrder = 'desc';
                    }
                    
                    document.querySelectorAll('.data-table th').forEach(h => h.classList.remove('sorted'));
                    th.classList.add('sorted');
                    th.querySelector('.sort-icon').textContent = state.sortOrder === 'asc' ? '↑' : '↓';
                    
                    loadData();
                });
            });
        }
        
        function escapeHtml(str) {
            if (!str) return '';
            return str.replace(/[&<>"']/g, m => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }[m]));
        }
        
        function formatDate(dateStr) {
            if (!dateStr) return '-';
            const date = new Date(dateStr);
            const now = new Date();
            const diff = now - date;
            
            if (diff < 60000) return 'Just now';
            if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
            if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
            if (diff < 604800000) return Math.floor(diff / 86400000) + 'd ago';
            
            return date.toLocaleDateString();
        }
        
        document.addEventListener('DOMContentLoaded', () => {
            document.querySelectorAll('.nav-item').forEach(item => {
                item.addEventListener('click', () => switchTab(item.dataset.tab));
            });
            
            document.getElementById('search-input').addEventListener('input', e => {
                state.searchQuery = e.target.value;
                loadData();
            });
            
            document.getElementById('refresh-btn').addEventListener('click', loadData);
            document.getElementById('test-all-btn').addEventListener('click', toggleTesting);
            
            document.getElementById('select-all-configs').addEventListener('click', () => toggleSelectAll('select-all-configs'));
            document.getElementById('select-all-working').addEventListener('click', () => toggleSelectAll('select-all-working'));
            document.getElementById('select-all-retired').addEventListener('click', () => toggleSelectAll('select-all-retired'));
            
            document.addEventListener('click', hideContextMenu);
            document.addEventListener('keydown', e => {
                if (e.key === 'Escape') {
                    closeModal();
                    hideContextMenu();
                }
            });
            
            setupSorting();
            loadData();
            
            setInterval(loadData, 30000);
        });
    </script>
</body>
</html>
'''

class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data: Any, status: int = 200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_html(self, html: str):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode())

    def read_body(self) -> Dict:
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length:
            body = self.rfile.read(content_length)
            return json.loads(body.decode())
        return {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == '/' or path == '/index.html':
            self.send_html(HTML_TEMPLATE)

        elif path == '/api/stats':
            total = db.fetchone("SELECT COUNT(*) as c FROM configs")['c']
            working = db.fetchone("SELECT COUNT(*) as c FROM configs WHERE status = 'working'")['c']
            failed = db.fetchone("SELECT COUNT(*) as c FROM configs WHERE status = 'failed'")['c']
            retired = db.fetchone("SELECT COUNT(*) as c FROM configs WHERE status = 'retired'")['c']
            untested = db.fetchone("SELECT COUNT(*) as c FROM configs WHERE status = 'untested'")['c']
            subs = db.fetchone("SELECT COUNT(*) as c FROM subscriptions")['c']
            
            self.send_json({
                'total': total,
                'working': working,
                'failed': failed,
                'retired': retired,
                'untested': untested,
                'subscriptions': subs
            })

        elif path == '/api/subscriptions':
            subs = SubscriptionManager.get_all_subscriptions()
            self.send_json(subs)

        elif path == '/api/configs':
            status = query.get('status', [''])[0]
            search = query.get('search', [''])[0]
            sort_by = query.get('sort_by', ['id'])[0]
            sort_order = query.get('sort_order', ['desc'])[0]
            sub_id = query.get('subscription_id', [None])[0]
            
            configs = ConfigManager.get_all_configs(
                status_filter=status if status else None,
                search=search,
                sort_by=sort_by,
                sort_order=sort_order,
                subscription_id=int(sub_id) if sub_id else None
            )
            self.send_json(configs)

        elif path == '/api/settings':
            settings = SettingsManager.get_settings()
            self.send_json(asdict(settings))

        elif path == '/api/qrcode':
            uri = query.get('uri', [''])[0]
            svg = generate_qr_code_svg(uri)
            self.send_json({'svg': svg})

        elif path == '/api/test/progress':
            runner = TestRunner()
            progress = runner.test_progress.copy()
            if 'configs' in progress and len(progress['configs']) > 100:
                progress['configs'] = progress['configs'][:100]
            self.send_json(progress)

        else:
            self.send_json({'error': 'Not found'}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_body()

        if path == '/api/subscriptions':
            name = body.get('name', '').strip()
            url = body.get('url', '').strip()
            
            if not name or not url:
                self.send_json({'error': 'Name and URL required'}, 400)
                return
            
            sub_id = SubscriptionManager.add_subscription(name, url)
            if sub_id:
                self.send_json({'id': sub_id})
            else:
                self.send_json({'error': 'Subscription already exists'}, 400)

        elif path.startswith('/api/subscriptions/') and path.endswith('/update'):
            sub_id = int(path.split('/')[3])
            result = SubscriptionManager.update_subscription(sub_id)
            self.send_json(result)

        elif path == '/api/configs':
            uris = body.get('uris', [])
            added = ConfigManager.add_configs_bulk(uris)
            self.send_json({'added': added})

        elif path == '/api/configs/restore':
            ids = body.get('ids', [])
            ConfigManager.restore_configs(ids)
            self.send_json({'success': True})

        elif path == '/api/configs/retire_working':
            ConfigManager.retire_working_configs()
            self.send_json({'success': True})

        elif path == '/api/test':
            config_ids = body.get('config_ids')
            runner = TestRunner()
            result = runner.test_configs(config_ids)
            self.send_json(result)

        elif path == '/api/test/stop':
            runner = TestRunner()
            result = runner.stop_tests()
            self.send_json(result)

        elif path == '/api/settings':
            SettingsManager.save_settings(body)
            self.send_json({'success': True})

        else:
            self.send_json({'error': 'Not found'}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_body()

        if path.startswith('/api/subscriptions/'):
            sub_id = int(path.split('/')[3])
            SubscriptionManager.delete_subscription(sub_id, delete_configs=True)
            self.send_json({'success': True})

        elif path == '/api/configs':
            ids = body.get('ids', [])
            ConfigManager.delete_configs(ids)
            self.send_json({'success': True})

        else:
            self.send_json({'error': 'Not found'}, 404)

def run_server(host: str = '0.0.0.0', port: int = 8080):
    scheduler = BackgroundScheduler()
    scheduler.start()

    server = HTTPServer((host, port), RequestHandler)
    print(f"""
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║   ███████╗██╗   ██╗██████╗ ██╗██╗  ██╗                       ║
║   ██╔════╝██║   ██║██╔══██╗██║╚██╗██╔╝                       ║
║   ███████╗██║   ██║██████╔╝██║ ╚███╔╝                        ║
║   ╚════██║██║   ██║██╔══██╗██║ ██╔██╗                        ║
║   ███████║╚██████╔╝██████╔╝██║██╔╝ ██╗                       ║
║   ╚══════╝ ╚═════╝ ╚═════╝ ╚═╝╚═╝  ╚═╝  STUDIO              ║
║                                                               ║
║   V2Ray Subscription Testing Suite                            ║
║   ─────────────────────────────────────────────────────────   ║
║   Server running at: http://{host}:{port:<5}                    ║
║   Press Ctrl+C to stop                                        ║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝
""")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        scheduler.stop()
        server.shutdown()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Subix Studio - V2Ray Subscription Testing Suite')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--port', type=int, default=8080, help='Port to listen on')
    args = parser.parse_args()
    
    run_server(args.host, args.port)