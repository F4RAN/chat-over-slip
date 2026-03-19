ROOT_DIR="$HOME/Desktop/Projects/chat-over-dnstt"
python -m pip install -r "$ROOT_DIR/build/requirements-build.txt"
python -m PyInstaller --noconfirm --clean "$ROOT_DIR/build/chat_over_dnstt.spec"