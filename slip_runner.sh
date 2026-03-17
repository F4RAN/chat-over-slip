IP=$1
cd ~/Desktop/slipstream-rust && \
cargo run -p slipstream-client -- \
  --tcp-listen-port 8000 \
  --resolver ${IP}:53 \
  --domain t.qtn.at &
sleep 3
 ssh -D 192.168.1.3:1080 -o "ProxyCommand nc 127.0.0.1 8000" -o "StrictHostKeyChecking=no" f4ran@t.qtn.at -t 'echo "Slipstream connected by F4RAN"; bash'
# sshpass -p 123321 ssh -tt \
#    -o StrictHostKeyChecking=no \
#    -o ConnectTimeout=180 \
#    -o ServerAliveInterval=30 \
#    -o ServerAliveCountMax=10 \
#    -o TCPKeepAlive=yes \
#    -p 7000 \
#    -D 0.0.0.0:1080 \
#    f4ran@127.0.0.1
