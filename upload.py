#!/usr/bin/env python
# Upload a firmware to a D-Link DIR-615 E1 running in firmware upgrade mode

import sys
import os
import base64
import socket
import time

filename = sys.argv[1]
with open(filename, "rb") as f:
    content = f.read()

# Warm up the connection by fetching /
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
s.connect(("192.168.0.1", 80))
time.sleep(0.5)
rf = s.makefile("rb")
wf = s.makefile("wb")
wf.write("""GET / HTTP/1.1\r\nHost: 192.168.0.1\r\nConnection: close\r\n\r\n""")
wf.flush()
for line in rf:
    print(repr(line))
wf.close()
rf.close()
s.close()

print("Uploading...")

boundary = base64.b64encode(os.urandom(16))

data = dict(boundary=boundary, filename="C:\\foo\\bar\\" + os.path.basename(filename), content=content)

inner_content = """\
--%(boundary)s
Content-Disposition: form-data; name="files"; filename="%(filename)s"
Content-Type: application/octet-stream

%(content)s
--%(boundary)s--
""".replace("\n", "\r\n") % data

data['inner_size'] = len(inner_content)
data['inner_content'] = inner_content

header = """\
POST /cgi/index HTTP/1.1
Accept: image/gif, image/x-xbitmap, image/jpeg, image/pjpeg, application/x-shockwave-flash, */*
Referer: http://192.168.0.1/
Accept-Language: en-ca
Content-Type: multipart/form-data; boundary=%(boundary)s
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1; SV1)
Host: 192.168.0.1
Content-Length: %(inner_size)d
Connection: close
Cache-Control: no-cache

""".replace("\n", "\r\n") % data

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)     # Important
s.connect(("192.168.0.1", 80))
time.sleep(1.0)     # important
wf = s.makefile("wb")
rf = s.makefile("rb")
wf.write(header)
wf.flush()
time.sleep(1.0)
for i in range(0, len(inner_content), 1024):
    print("%d of %d" % (i, len(inner_content)))
    wf.write(inner_content[i:i+1024])
wf.write("\r\n")    # XXX - needed?
wf.flush()
time.sleep(1.0)
s.shutdown(socket.SHUT_WR)
print("done writing")
for line in rf:
    print(repr(line))
