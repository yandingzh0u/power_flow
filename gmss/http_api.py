"""One local HTTP server for the UI, folder uploads, jobs, and SSE logs."""
from __future__ import annotations
import argparse
from functools import partial
import hashlib
import gzip
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import ALGORITHMS, ROOT
from .service import JobService


class Handler(SimpleHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def end_headers(self):
        if not urlparse(self.path).path.startswith(('/api/', '/health')):
            self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def send_head(self):
        if urlparse(self.path).path in ('/', '/index.html'):
            root = Path(self.directory)
            html = (root / 'index.html').read_text(encoding='utf-8')
            for name in ('styles.css', 'trace.js', 'app.js'):
                version = hashlib.sha256((root / name).read_bytes()).hexdigest()[:16]
                html = html.replace(f'{name}?v=__ASSET_VERSION__', f'{name}?v={version}')
            data = html.encode('utf-8')
            # Always send current HTML, including when the browser has an old page.
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            return BytesIO(data)
        return super().send_head()

    @property
    def service(self):
        return self.server.service

    def send_json(self, code, payload, filename=None):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        compress = False
        for item in self.headers.get('Accept-Encoding', '').split(','):
            name, *options = item.strip().split(';')
            if name != 'gzip':
                continue
            quality = next((p.strip()[2:] for p in options if p.strip().startswith('q=')), '1')
            try:
                compress = float(quality) > 0 and len(data) >= 1024
            except ValueError:
                pass
        if compress:
            data = gzip.compress(data, compresslevel=1)
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Vary', 'Accept-Encoding')
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        if compress:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self, limit=48 * 1024**2):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= limit:
            self.close_connection = True
            raise ValueError('请求为空或超过上传大小限制')
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError('上传内容不完整，请重新上传')
        return data

    def payload(self):
        return json.loads(self.read_body())

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ('/health', '/api/v1/health'):
                return self.send_json(200, {'status':'ok'})
            if path == '/api/datasets':
                return self.send_json(200, {'datasets':self.service.datasets(), 'algorithms':ALGORITHMS})
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[:2] == ['api','runs']:
                task = self.service.get(parts[2])
                if parts[3] == 'events':
                    return self.stream(task)
                if parts[3] == 'result':
                    if task['status'] != 'completed':
                        return self.send_json(409, {'status':task['status']})
                    return self.send_json(200, self.service.result(task))
                if parts[3] == 'trace':
                    # A plain HTTP download preserves full structured telemetry,
                    # even if the browser's visible log has been cleared.
                    with task['condition']:
                        snapshot = {'task_id': task['id'], 'status': task['status'], 'events': list(self.service.events(task))}
                    return self.send_json(200, snapshot, f"powerflow-{task['id']}-trace.json")
            if path.startswith('/api/'):
                return self.send_json(404, {'error':'接口不存在'})
            return super().do_GET()
        except KeyError as error:
            self.send_json(404, {'error':str(error)})
        except (ValueError, TypeError) as error:
            self.send_json(400, {'error':str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path == '/api/datasets/import-archive':
                name = parse_qs(urlparse(self.path).query).get('name', [''])[0]
                return self.send_json(201, {'dataset': self.service.import_archive(name, self.read_body(32 * 1024**2))})
            payload = self.payload()
            if path == '/api/datasets/import':
                return self.send_json(201, {'dataset':self.service.import_folder(payload.get('files'))})
            if path == '/api/runs':
                task = self.service.create(str(payload.get('dataset_id') or ''), str(payload.get('algorithm','partitioned-nr')),
                                           str(payload.get('snapshot_id') or ''))
                return self.send_json(202, {'task_id':task['id'], 'status':task['status'],
                                            'dataset_id':task['dataset_id'], 'snapshot_id':task['snapshot_id']})
            return self.send_json(404, {'error':'接口不存在'})
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            self.send_json(400, {'error':str(error)})
        except Exception as error:
            self.send_json(500, {'error':str(error)})

    def stream(self, task):
        if task.get('archived'):
            task = {**task, 'events': self.service.events(task), 'event_frames': None}
        last = self.headers.get('Last-Event-ID','0')
        index = max(0, int(last))
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream; charset=utf-8')
        self.send_header('Cache-Control','no-cache')
        self.send_header('Connection','close')
        self.send_header('X-Accel-Buffering','no')
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                with task['condition']:
                    if index >= len(task['events']) and task['status'] not in ('completed','failed'):
                        task['condition'].wait(timeout=1)
                    events = task['events'][index:]
                    frames = task.get('event_frames')
                    frames = frames[index:] if frames is not None else [None] * len(events)
                for event, frame in zip(events, frames):
                    if frame is None:
                        data = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                        frame = f"id: {event['id']}\ndata: {data}\n\n".encode()
                    self.wfile.write(frame)
                    index = event['id']
                self.wfile.write(b': heartbeat\n\n')
                self.wfile.flush()
                if task['status'] in ('completed','failed') and index >= len(task['events']):
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass


def make_server(host='127.0.0.1', port=4173, runtime=None, raw_dir=None):
    server = ThreadingHTTPServer((host,port), partial(Handler, directory=str(ROOT/'web_sjz')))
    server.daemon_threads = True
    server.service = JobService(runtime, raw_dir)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=4173)
    args = parser.parse_args()
    server = make_server(args.host,args.port)
    print(f'石家庄潮流系统：http://{args.host}:{server.server_port}/',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.service.executor.shutdown(wait=True)


if __name__ == '__main__':
    main()
