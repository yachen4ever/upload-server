#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最简单文件上传服务器（仅标准库，Python 3.6+ 可用，CentOS 7 自带 python3 可跑）。

用法:
    python3 upload_server.py [保存目录] [端口]
默认保存到 /srv，监听 0.0.0.0:1983。
浏览器打开 http://<服务器IP>:1983，选择文件上传即可。

注意: 无任何鉴权，请只放在可信内网使用。
"""
import html
import os
import socketserver
import sys
import time
from urllib.parse import unquote
from http.server import BaseHTTPRequestHandler, HTTPServer

# ThreadingHTTPServer 要 3.7 才有（CentOS 7 的 python3 是 3.6），这里等价手写一个
class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

SAVE_DIR = sys.argv[1] if len(sys.argv) > 1 else "/srv"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 1983

PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>文件上传</title>
</head>
<body style="font-family: sans-serif; max-width: 480px; margin: 40px auto">
<h3>上传文件到服务器</h3>
<form method="post" action="/" enctype="multipart/form-data">
<p><input type="file" name="file" required></p>
<p><button type="submit">上传</button></p>
</form>
<hr>
<p>{saved}</p>
</body>
</html>
"""


class UploadError(Exception):
    """协议层面的上传错误（回 400）。"""


def get_boundary(ctype):
    for seg in ctype.split(";"):
        seg = seg.strip()
        if seg.lower().startswith("boundary="):
            b = seg[9:].strip()
            if len(b) >= 2 and b[0] == '"' and b[-1] == '"':
                b = b[1:-1]
            return b.encode("latin-1")
    return None


def _parse_disposition(disp):
    """从 Content-Disposition 值解析 name / filename。"""
    name = filename = None
    fn_star = None
    text = disp.decode("utf-8", "surrogateescape")
    for seg in text.split(";")[1:]:
        seg = seg.strip()
        if "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1].replace('\\"', '"')
        if k == "name":
            name = v
        elif k == "filename":
            filename = v
        elif k == "filename*" and v:
            fn_star = v
    if fn_star:  # RFC 5987: charset'lang'percent-encoded
        try:
            charset, _, rest = fn_star.partition("'")
            _lang, _, value = rest.partition("'")
            filename = unquote(value, encoding=charset or "utf-8", errors="replace")
        except Exception:
            pass
    return name, filename


def _is_boundary(line, boundary):
    s = line.rstrip(b"\r\n").rstrip(b" \t")
    if s == b"--" + boundary:
        return "part"
    if s == b"--" + boundary + b"--":
        return "final"
    return None


def _strip_delim_crlf(b):
    # 分隔符前面的那个 CRLF 属于分隔符，不属于文件内容
    if b.endswith(b"\r\n"):
        return b[:-2]
    if b.endswith(b"\r") or b.endswith(b"\n"):
        return b[:-1]
    return b


def parse_and_save(rfile, boundary, save_dir):
    """流式解析 multipart/form-data，保存 name="file" 部分。

    返回 (保存的文件名, 字节数)；协议错误抛 UploadError。
    """
    readline = rfile.readline
    while True:  # 跳过前导内容，定位第一个 boundary
        line = readline(1 << 16)
        if not line:
            raise UploadError("请求体中没有 boundary")
        if _is_boundary(line, boundary) == "part":
            break

    saved_name = None
    saved_size = 0
    kind = None
    while True:
        disp = None
        while True:  # 读 part 头
            line = readline(1 << 16)
            if not line:
                raise UploadError("请求被截断")
            line = line.rstrip(b"\r\n")
            if not line:
                break
            if disp is None and line.lower().startswith(b"content-disposition:"):
                disp = line[len(b"content-disposition:"):].strip()
        name, filename = _parse_disposition(disp or b"")

        dest = None
        fobj = None
        if name == "file" and filename:
            safe = os.path.basename(filename.replace("\\", "/")).strip()
            if not safe or safe in (".", ".."):
                safe = time.strftime("upload_%Y%m%d_%H%M%S.bin")
            dest = os.path.join(save_dir, safe)
            if os.path.exists(dest):  # 同名不覆盖，自动加时间戳
                root, ext = os.path.splitext(safe)
                dest = os.path.join(save_dir, root + time.strftime("_%Y%m%d_%H%M%S") + ext)
            fobj = open(dest, "wb")
        try:
            size = 0
            prev = None
            while True:  # 流式写文件 / 丢弃非 file 字段，直到下一个 boundary
                line = readline(1 << 16)
                if not line:
                    raise UploadError("请求被截断（上传未完成）")
                kind = _is_boundary(line, boundary)
                if kind:
                    if prev is not None:
                        chunk = _strip_delim_crlf(prev)
                        if fobj:
                            fobj.write(chunk)
                        size += len(chunk)
                    break
                if prev is not None:
                    if fobj:
                        fobj.write(prev)
                    size += len(prev)
                prev = line
        except Exception:
            if fobj:
                fobj.close()
            if dest:  # 上传中断，删掉半截文件
                try:
                    os.remove(dest)
                except OSError:
                    pass
            raise
        fobj.close() if fobj else None
        if dest and saved_name is None:
            saved_name = os.path.basename(dest)
            saved_size = size
        if kind == "final":
            break
    if saved_name is None:
        raise UploadError("没有找到文件（请从页面的表单上传）")
    return saved_name, saved_size


class UploadHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _html(self, code, saved, close=False):
        data = PAGE.format(saved=saved).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _drain(self, n):
        while n > 0:
            chunk = self.rfile.read(min(n, 65536))
            if not chunk:
                return
            n -= len(chunk)

    def do_GET(self):
        self._html(200, "保存目录: " + html.escape(SAVE_DIR))

    def do_POST(self):
        try:
            clen = int(self.headers.get("Content-Length") or 0)
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype.lower():
                self._drain(clen)  # 读完请求体再回包，避免复用连接被污染
                self._html(400, "错误: 请通过页面表单上传", close=True)
                return
            boundary = get_boundary(ctype)
            if not boundary:
                self._drain(clen)
                self._html(400, "错误: 缺少 multipart boundary", close=True)
                return
            name, size = parse_and_save(self.rfile, boundary, SAVE_DIR)
            self._html(200, '上传成功: <b>%s</b> (%d 字节)　<a href="/">继续上传</a>'
                       % (html.escape(name), size))
        except UploadError as e:
            self._html(400, "错误: " + html.escape(str(e)), close=True)
        except Exception as e:
            try:
                self._html(500, "错误: " + html.escape(str(e)), close=True)
            except Exception:
                pass


def main():
    os.makedirs(SAVE_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), UploadHandler)
    print("保存目录: %s" % os.path.abspath(SAVE_DIR))
    print("监听: http://0.0.0.0:%d （浏览器访问 http://<本机IP>:%d）" % (PORT, PORT))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
