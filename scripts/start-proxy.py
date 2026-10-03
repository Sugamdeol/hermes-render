"""Start the authenticated public proxy before any slow state restoration."""
import hashlib
import os
from pathlib import Path
import subprocess
import crypt

password = os.environ.get("HERMES_GATEWAY_TOKEN", "")
if not password:
    raise SystemExit("HERMES_GATEWAY_TOKEN is required for dashboard access")
port = int(os.environ.get("PORT", "10000"))
Path("/etc/nginx/hermes.htpasswd").write_text("hermes:" + crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512)) + "\n")
Path("/etc/nginx/hermes.htpasswd").chmod(0o640)
subprocess.run(["chown", "root:www-data", "/etc/nginx/hermes.htpasswd"], check=True)
Path("/etc/nginx/nginx.conf").write_text('''user www-data;
worker_processes 1;
pid /run/nginx.pid;
events { worker_connections 128; }
http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  access_log off;
  error_log /dev/stderr warn;
  map $http_upgrade $connection_upgrade { default upgrade; '' close; }
  server {
    listen PORT_VALUE;
    client_max_body_size 20m;
    auth_basic "Hermes";
    auth_basic_user_file /etc/nginx/hermes.htpasswd;
    location = /healthz {
      auth_basic off;
      proxy_pass http://127.0.0.1:9119/api/status;
    }
    location = /telegram {
      auth_basic off;
      proxy_pass http://127.0.0.1:8443/telegram;
    }
    location / {
      proxy_pass http://127.0.0.1:9119;
      proxy_http_version 1.1;
      proxy_set_header Host "127.0.0.1:9119";
      proxy_set_header Authorization "";
      proxy_set_header X-Forwarded-For "";
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_read_timeout 3600s;
      proxy_buffering off;
    }
  }
}
'''.replace("PORT_VALUE", str(port)))
subprocess.run(["nginx"], check=True)
