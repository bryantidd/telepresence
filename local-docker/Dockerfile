FROM alpine:3.6

RUN apk add --no-cache python3 openssh-client iptables tini

COPY sshuttle-telepresence /usr/bin
COPY entrypoint.py /usr/bin
COPY telepresence.py /usr/bin
RUN chmod +x /usr/bin/sshuttle-telepresence /usr/bin/entrypoint.py
ENTRYPOINT ["/sbin/tini", "-v", "--", "python3", "/usr/bin/entrypoint.py"]