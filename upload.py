#!/usr/bin/env python
# Dwayne Litzenberger <dlitz@dlitz.net>
# 2013-10-27
#
# This tool uploads a firmware image to a D-Link router running in its firmware
# upgrade mode, which is a special-purpose  firmware on some D-Link routers
# that allows you to load a new "main" firmware image onto the device, even if
# the device is otherwise "bricked".
#
# I've tested this on a D-Link DIR-615 E1, which self-reports as follows:
#
#     D-LINK Firmware Upgrade System
#     Version 1.0.0.0 (Language Pack Support)
#     Date 2009/07/15
#     File Path    [                    ] [Browse]
#     [Send]
#
# It also returns this HTTP header (more on that later):
#
#     Server: uIP/0.9 (http://dunkels.com/adam/uip/)
#
# To enter firmware update mode:
#
# 1. Plug your computer into one of the LAN ports on the router.  Configure
#    your computer to use an address between 192.168.0.2 and 192.168.0.254.
# 2. Unplug the router
# 3. While holding down the reset button, apply power to the router until its
#    power LED starts blinking amber.
#
# At that point, you're supposed to be able to go to http://192.168.0.1/ and
# upload the firmware via a simple web form.  This form is rumored to "only
# work on MSIE 6", but that's not the whole story.
#
# When I try to fetch the page with Firefox, or even just wget, the connection
# resets! (TCP RST).  However, if I use "telnet 192.168.0.1 80" and manually
# type "GET /", the router returns the HTML source.  Once you strip out the
# irrelevent cruft, the form basically just looks like this:
#
#    <form action="/cgi/index" enctype="multipart/form-data" method=post>
#       File Path
#       <input type=file name=files value="">
#       <input type="submit" value="Send">
#    </form>
#
# That HTML should work anywhere, not just in MSIE 6.
#
# After some experimentation, I figured out that after establishing a
# connection (SYN, SYN+ACK, ACK), you have to wait a little while before
# sending your HTTP request, or the router will break the connection (TCP RST).
#
# So this router has a broken network stack.  Interestingly, we can see the
# name of the network stack in the HTTP header:
#
#   Server: uIP/0.9 (http://dunkels.com/adam/uip/)
#
# uIP is basically an all-in-one network stack for really small embedded
# systems; Its source code is available on GitHub at
# https://github.com/adamdunkels/uip, and it's now been folded into Contiki.
#
# uIP apparently only ever sets the TCP Window field to the maximum window
# size, or to 0:
#
#   if(uip_connr->tcpstateflags & UIP_STOPPED) {
#     /* If the connection has issued uip_stop(), we advertise a zero
#        window so that the remote host will stop sending data. */
#     BUF->wnd[0] = BUF->wnd[1] = 0;
#   } else {
#     BUF->wnd[0] = ((UIP_RECEIVE_WINDOW) >> 8);
#     BUF->wnd[1] = ((UIP_RECEIVE_WINDOW) & 0xff);
#   }
#
#   /* https://github.com/adamdunkels/uip/blob/uip-0-9/uip/uip.c#L14560 */
#
# That's odd.  The window is supposed to be the number of bytes *remaining* in
# the buffer, not the total size of the buffer.  (With some trickery, it should
# still be possible for this to work correctly using another buffer external to
# uIP, so I'm not really sure whether this is a uIP bug or a bug in the
# firmware-updater.)
#
# Anyway, this apparently breaks when we send 1024 bytes to the router as two
# 512-byte segments.
#
#    IP 192.168.0.1.80 > 192.168.0.2.48596: Flags [.], ack 444, win 1024, length 0
#    IP 192.168.0.2.48596 > 192.168.0.1.80: Flags [P.], seq 444:956, ack 1, win 14600, length 512
#    IP 192.168.0.2.48596 > 192.168.0.1.80: Flags [P.], seq 956:1468, ack 1, win 14600, length 512
#    IP 192.168.0.1.80 > 192.168.0.2.48596: Flags [.], ack 444, win 1024, length 0
#    IP 192.168.0.1.80 > 192.168.0.2.48596: Flags [.], ack 444, win 1024, length 0
#
# Those last two last "ack 444, win 1024" segments are in response to the
# segments we sent, but they just look like retransmissions.  They should
# should either acknowledge the data we're sending ("ack 956", "ack 1468"), or
# set the window size to 0 to indicate that the receiver is not prepared to receive any more data. ("ack 444, win 0").
#
# So, 200ms later, after receiving no acknowledgment, we retransmit the first
# segment.  This time, we get an ACK for it:
#
#    IP 192.168.0.2.48596 > 192.168.0.1.80: Flags [P.], seq 444:956, ack 1, win 14600, length 512
#    IP 192.168.0.1.80 > 192.168.0.2.48596: Flags [.], ack 956, win 1024, length 0
#
# Our window is still 1024 bytes, so we send another 512-byte segment to fill
# up the remote's buffer.  Again, we get the wrong ACK:
#
#    IP 192.168.0.2.48627 > 192.168.0.1.80: Flags [P.], seq 1468:1980, ack 1, win 14600, length 512
#    IP 192.168.0.1.80 > 192.168.0.2.48627: Flags [.], ack 956, win 1024, length 0
#
# Since we can't tell that those ACKs are actually for the data that we sent,
# we assume that the dropped packets are caused by congestion and back off
# exponentially.  Pretty soon, we're delaying for 3, 6, 13, 26 seconds every
# time we fail to get an ACK.
#
# Additionally, there are the following bugs:
# - The header needs to be in a different TCP segment from the body, or the
#   firmware won't load properly.
# - We need to get our final data ACK before sending TCP FIN.
#
# So, we need this stupid program to work around these bugs.


import array
import base64
import sys
import os
import select
import socket
import time
from fcntl import ioctl
from termios import TIOCOUTQ as SIOCOUTQ    # On Linux, SIOCOUTQ has the same value as TIOCOUTQ

filename = sys.argv[1]
with open(filename, 'rb') as f:
    content = f.read()

# Build the request
data = {}
data['boundary'] = base64.b64encode(os.urandom(16))
data['filename'] = os.path.basename(filename).encode('utf-8')
data['content'] = content

header_template = b"""\
POST /cgi/index HTTP/1.1
Host: 192.168.0.1
Accept: */*
Content-Type: multipart/form-data; boundary={boundary}
Content-Length: {body_length}
Connection: close
Cache-Control: no-cache

""".replace(b'\n', b'\r\n')

body_template = b"""\
--{boundary}
Content-Disposition: form-data; name="files"; filename="{filename}"
Content-Type: application/octet-stream

{content}
--{boundary}--
""".replace(b'\n', b'\r\n')

body = data['body'] = body_template.format(**data)
data['body_length'] = len(data['body'])
header = header_template.format(**data)
request = header + body

# Connect, then wait a bit.
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
print("Connecting...")
s.connect(('192.168.0.1', 80))
print("Connected.  Sleeping.")
time.sleep(0.1)

# We're going to try hard not to have more than one segment in flight at a time.
# The plan is to reduce the outgoing buffer size until it can be fulfilled by a
# single request.
bufsize = 1024
s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, bufsize)
s.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
s.setblocking(False)

print('Uploading...')
i = 0
ioctl_buf = array.array('h', [0])
received = []
eof = False
while True:
    # Delay a tiny bit.  This shouldn't be necessary, and theoretically should
    # slow things down, but it seems to help avoid dropped segments and thus
    # makes the upload more reliable.  I haven't figured out the exact cause.
    time.sleep(0.01)

    # Wait until the send buffer isn't full.
    r, w, e = select.select([s] if not eof else [], [s], [])

    if s in r:
        # We're receiving a response from the remote side.  Stop sending to read the response.
        rcv = s.recv(65535)
        received.append(rcv)
        if rcv:
            print("\nReceived: %r" % (rcv,))
        else:
            print("\nConnection terminated after sending %d of %d bytes." % (i, len(request)))
            eof = True
        continue

    assert i <= len(request)

    # When we finish sending the header or the body, or when we receive EOF,
    # flush the output buffer (select() already returned, so we have to
    # busy-wait at this point).
    if i == len(header) or i == len(request) or eof:
        # Busy-wait until all data have been sent
        ioctl(s, SIOCOUTQ, ioctl_buf, 1)
        bytes_remaining = ioctl_buf[0]
        sys.stdout.write("\n")
        if bytes_remaining:
            print("Busy-waiting until all bytes are sent.")
        while bytes_remaining > 0:
            ioctl(s, SIOCOUTQ, ioctl_buf, 1)
            bytes_remaining = ioctl_buf[0]
        print("Output buffer drained.")

        if i == len(header):
            print("\nDone sending request header (%d bytes)" % (i,))
        elif i == len(request):
            print("\nDone uploading all %d bytes." % (i,))
            break
        else:
            assert eof
            print("\nDone uploading %d bytes (of %d total)." % (i, len(request)))

    # Send a block
    sys.stdout.write("Uploading %d bytes starting at %d\r" % (bufsize, i))
    sys.stdout.flush()
    if i < len(header):
        sent = s.send(header[i:i+bufsize])
        i += sent
    else:
        sent = s.send(request[i:i+bufsize])
        i += sent

    # If we didn't send the whole buffer, then we should adjust the buffer some more.
    if sent < bufsize and i != len(header) and i < len(request):
        print("\nRequested to send %d bytes, but only %d were sent.  Reducing buffer to %d." % (bufsize, sent, sent))
        bufsize = sent
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, bufsize)

# Shut down the sending side of the connection.  This is only safe to do after
# we've received ACKs for all of the data we've sent so far.
s.shutdown(socket.SHUT_WR)

# Re-enable blocking mode
s.setblocking(True)

# Read the rest of the response until EOF.
success = False

t0 = time.time()
if not eof:
    print("Waiting for EOF from device.")
    rf = s.makefile('rb')
    for line in rf:
        received.append(line)
        print(repr(line))

# Done.
s.close()

if b"Device is Upgrading the Firmware" in b"".join(received):
    success = True

# Wait about 100 seconds for the firmware upgrade to complete
if success:
    wait_seconds = max(0.0, 100.0 - (time.time() - t0))
    sys.stdout.write("\n")
    for i in range(int(wait_seconds)):
        sys.stdout.write("Please wait: %d%%\r" % (100.0 * (i+1) / wait_seconds,))
        sys.stdout.flush()
        time.sleep(1.0)
    sys.stdout.write("\n")
    print("Done.  The new firmware should be installed now.")
