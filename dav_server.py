# WebDAV server — HF persistent storage (/data) ko file manager se browse karne ke liye.
# 127.0.0.1:8504 par chalta hai, nginx /dav ke peeche. Auth Cloudflare Worker karta hai
# (private Space ka HF gate + Worker ka user/pass), isliye yahan alag auth nahi.
import os
from wsgidav.wsgidav_app import WsgiDAVApp
from cheroot import wsgi

ROOT = "/data" if os.path.isdir("/data") else os.getcwd()

app = WsgiDAVApp({
    "provider_mapping": {"/dav": ROOT},
    "simple_dc": {"user_mapping": {"*": True}},
    "http_authenticator": {
        "domain_controller": None,
        "accept_basic": True,
        "accept_digest": False,
        "default_to_digest": False,
    },
    "dir_browser": {"enable": False},
    "verbose": 1,
})

server = wsgi.Server(bind_addr=("127.0.0.1", 8504), wsgi_app=app)
try:
    server.start()
except KeyboardInterrupt:
    server.stop()
