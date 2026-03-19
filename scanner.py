import subprocess
import os
import fcntl
import time
import threading
import argparse
from concurrent.futures import ThreadPoolExecutor

# --- CONFIGURATION ---
THREADS_COUNT = 100
TIMEOUT = 10
# ---------------------

file_lock = threading.Lock()
counter_lock = threading.Lock()
processed_count = 0

def run_dns_ssh(ip, thread_index, total_ips, output_file):
    global processed_count
    
    local_port = 7000 + thread_index
    socks_port = 8080 + thread_index
    
    kill_cmd = f"kill -9 $(lsof -t -i:{local_port}) 2>/dev/null || true"
    subprocess.run(kill_cmd, shell=True)

    cmd = f"""
    dnstt -udp {ip}:53 -pubkey-file ~/dnstt/server.pub t.pleight.app 127.0.0.1:{local_port} &
    sleep 2
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 -p {local_port} -D192.168.1.3:{socks_port} f4ran@127.0.0.1
    """

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        preexec_fn=os.setsid
    )

    for pipe in (process.stdout, process.stderr):
        fd = pipe.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    accumulated_log = ""
    start_time = time.time()
    success = False

    while time.time() - start_time < TIMEOUT:
        for pipe in (process.stdout, process.stderr):
            try:
                chunk = pipe.read()
                if chunk: accumulated_log += chunk
            except: pass

        if "begin stream" in accumulated_log.lower():
            duration = round(time.time() - start_time, 2)
            with file_lock:
                with open(output_file, "a") as res_file:
                    res_file.write(f"IP: {ip} - Time: {duration}s\n")
            success = True
            break
        time.sleep(0.5)

    try:
        os.killpg(os.getpgid(process.pid), 9)
    except:
        pass

    with counter_lock:
        processed_count += 1
        print(f"[{processed_count}/{total_ips}] {'[+] SUCCESS' if success else '[-] FAILED'} : {ip}", flush=True)

def main():
    parser = argparse.ArgumentParser(description="DNS tunnel scanner")
    parser.add_argument("-f", "--from", dest="input_file", required=True, help="Input file with IPs (one per line)")
    parser.add_argument("-o", "--output", dest="output_file", default="result.txt", help="Output file for results (default: result.txt)")
    args = parser.parse_args()
    input_file = args.input_file
    output_file = args.output_file

    if not os.path.exists(input_file):
        print("txt file not found!")
        return

    with open(input_file, "r") as f:
        ips = [line.strip() for line in f if line.strip()]

    total_ips = len(ips)
    print(f"Starting scan with {THREADS_COUNT} threads on {total_ips} IPs...\n")
    
    with ThreadPoolExecutor(max_workers=THREADS_COUNT) as executor:
        for index, ip in enumerate(ips):
            thread_idx = index % THREADS_COUNT
            executor.submit(run_dns_ssh, ip, thread_idx, total_ips, output_file)

if __name__ == "__main__":
    main()