FROM alpine
RUN apk add --no-cache iptables python3 && adduser -D -u 10001 proxy
COPY rules.sh /rules.sh
COPY proxy.py /proxy.py
