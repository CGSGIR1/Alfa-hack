"""Serve only the explicitly registered public videos, including byte ranges."""
import re


def byte_range(header, size):
    if not header:
        return 0, size - 1, False
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header.strip())
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError('Invalid byte range')
    first, last = match.groups()
    if first:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    else:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError('Invalid suffix')
        start, end = max(0, size - suffix), size - 1
    if start >= size or end < start:
        raise ValueError('Unsatisfiable range')
    return start, end, True


def send_video(handler, path, head=False):
    if not path.is_file():
        handler.send_response(404)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
        return
    with path.open('rb') as stream:
        size = path.stat().st_size
        try:
            start, end, partial = byte_range(handler.headers.get('Range'), size)
        except ValueError:
            handler.send_response(416)
            handler.send_header('Content-Range', f'bytes */{size}')
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return
        handler.send_response(206 if partial else 200)
        handler.send_header('Content-Type', 'video/mp4')
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('Content-Length', str(end - start + 1))
        handler.send_header('Cache-Control', 'public, max-age=3600')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        if partial:
            handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        handler.end_headers()
        if head:
            return
        try:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                data = stream.read(min(256 * 1024, remaining))
                if not data:
                    break
                handler.wfile.write(data)
                remaining -= len(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            handler.close_connection = True
