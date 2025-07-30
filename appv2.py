# app.py
import os
import sys
import time
import signal
import argparse
from typing import List
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
import colorama  # pip install colorama
import threading
import queue  # For log queueing and input queueing
import json  # For safe JSON encoding
import redis  # pip install redis

# Flask imports (pip install flask)
from flask import Flask, Response, jsonify, request, render_template_string

colorama.init(autoreset=True)

# Global variables for sharing state between CLI and Flask
current_pod = None  # Will hold {'name': str, 'namespace': str}
log_queue = queue.Queue()  # Queue to hold log lines for Flask
input_queue = queue.Queue()  # Queue for user input in CLI
stop_event = threading.Event()  # To signal stopping the log stream
stream_thread = None  # Global stream thread

# Redis setup
redis_client = redis.Redis(host='redis', port=6379, db=0, decode_responses=True)

# Allowed namespaces for restriction
ALLOWED_NAMESPACES = ["kavosh", "secure", "bizagi"]

# Flask app setup
app = Flask(__name__)

@app.route('/logs')
def logs():
    """SSE endpoint for streaming logs with improved real-time handling."""
    def generate():
        yield 'data: {"message": "Connected to log stream"}\n\n'
        last_yield_time = time.time()
        has_yielded_waiting = False
        while not stop_event.is_set():
            try:
                line = log_queue.get(timeout=0.05)  # Short timeout for responsiveness
                if line == '__END__':  # Sentinel to end stream
                    break
                # Yield as JSON to avoid parse errors in JS
                yield f'data: {json.dumps({"log": line, "timestamp": time.time()})}\n\n'  # Add timestamp for live feel
                last_yield_time = time.time()
                # Do not reset has_yielded_waiting here to avoid repeating "Waiting for logs..." after each log
            except queue.Empty:
                current_time = time.time()
                if current_pod is None:
                    yield f'data: {json.dumps({"message": "No pod selected. Select a pod in the terminal or via web."})}\n\n'
                    last_yield_time = current_time
                elif not has_yielded_waiting:
                    yield f'data: {json.dumps({"message": "Waiting for logs..."})}\n\n'
                    last_yield_time = current_time
                    has_yielded_waiting = True  # Only yield waiting once per connection
                elif current_time - last_yield_time > 10:  # Heartbeat every 10 seconds for liveness
                    yield ': heartbeat\n\n'
                    last_yield_time = current_time
                time.sleep(0.05)  # Short sleep for improved responsiveness
        yield f'data: {json.dumps({"message": "Log stream ended. Select another pod in terminal or web."})}\n\n'
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/namespaces')
def get_namespaces():
    """API to get list of allowed namespaces only."""
    v1 = client.CoreV1Api()
    try:
        namespaces = v1.list_namespace().items
        ns_list = [ns.metadata.name for ns in namespaces if ns.metadata.name in ALLOWED_NAMESPACES]
        return jsonify(ns_list)
    except ApiException as e:
        return jsonify({"error": str(e)}), 500

@app.route('/pods/<namespace>')
def get_pods_in_namespace(namespace):
    """API to get list of pods in a namespace (only if allowed)."""
    if namespace not in ALLOWED_NAMESPACES:
        return jsonify({"error": "Namespace not allowed"}), 403
    pods = list_pods(namespace)
    pod_list = [{"name": p["name"], "namespace": p["namespace"]} for p in pods]
    return jsonify(pod_list)

@app.route('/select_pod/<namespace>/<pod_name>')
def select_pod(namespace, pod_name):
    """Select a pod and start streaming its logs (clear cache and start fresh, only if namespace allowed)."""
    if namespace not in ALLOWED_NAMESPACES:
        return "Namespace not allowed", 403
    print(f"{colorama.Fore.YELLOW}Pod selected from web: {pod_name} in {namespace}")
    stop_log_streaming()
    
    # Clear the queue to avoid any old logs from previous pods
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break
    
    # Do not load cached logs; we want to start fresh
    # Cache will be cleared in start_log_streaming
    
    start_log_streaming(pod_name, namespace)
    return "OK"

@app.route('/')
def index():
    """Main page displaying namespaces."""
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Kubernetes Pod Logs</title>
        <style>
            body { font-family: monospace; background: #f0f0f0; padding: 20px; }
            .list { border: 1px solid #ccc; padding: 10px; margin-bottom: 20px; background: white; }
            #logs { border: 1px solid #ccc; padding: 10px; height: 400px; overflow-y: scroll; background: white; }
        </style>
    </head>
    <body>
        <h1>Real-Time Pod Logs - Namespaces</h1>
        <div id="namespace-list" class="list">Loading namespaces...</div>
        <div id="pod-list" class="list" style="display: none;">Loading pods...</div>
        <div id="logs">Connecting to stream...</div>
        <button onclick="loadNamespaces()">Refresh Namespaces</button>
        <script>
            let logsDiv = document.getElementById('logs');
            let namespaceListDiv = document.getElementById('namespace-list');
            let podListDiv = document.getElementById('pod-list');
            let evtSource = new EventSource("/logs");

            evtSource.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    if (data.message) {
                        logsDiv.innerHTML += '<p style="color: gray;">' + data.message + '</p>';
                    } else if (data.log) {
                        const ts = new Date(data.timestamp * 1000).toLocaleTimeString();
                        logsDiv.innerHTML += '<p><span style="color: blue;">[' + ts + ']</span> ' + data.log.replace(/\\n/g, '<br>') + '</p>';  // Add timestamp for live display
                    } else {
                        logsDiv.innerHTML += '<p>' + event.data + '</p>';  // Fallback
                    }
                } catch (e) {
                    logsDiv.innerHTML += '<p style="color: red;">Error parsing log: ' + event.data + '</p>';
                }
                logsDiv.scrollTop = logsDiv.scrollHeight;  // Auto-scroll
            };
            evtSource.onerror = function() {
                logsDiv.innerHTML += '<p style="color: red;">Connection lost. Reconnecting...</p>';
            };

            function loadNamespaces() {
                fetch('/namespaces')
                    .then(response => response.json())
                    .then(namespaces => {
                        let html = '<h2>Available Namespaces (Click to view pods)</h2><ul>';
                        namespaces.forEach(ns => {
                            html += `<li><a href="#" onclick="loadPods('${ns}')">${ns}</a></li>`;
                        });
                        html += '</ul>';
                        namespaceListDiv.innerHTML = html;
                        podListDiv.style.display = 'none';
                    })
                    .catch(error => {
                        namespaceListDiv.innerHTML = '<p style="color: red;">Error loading namespaces: ' + error + '</p>';
                    });
            }

            function loadPods(ns) {
                fetch(`/pods/${ns}`)
                    .then(response => response.json())
                    .then(pods => {
                        let html = `<h2>Pods in ${ns} (Click to view logs)</h2><ul>`;
                        pods.forEach(pod => {
                            html += `<li><a href="#" onclick="selectPod('${ns}', '${pod.name}')">${pod.name}</a></li>`;
                        });
                        html += '</ul><button onclick="backToNamespaces()">Back to Namespaces</button>';
                        podListDiv.innerHTML = html;
                        podListDiv.style.display = 'block';
                        namespaceListDiv.style.display = 'none';
                    })
                    .catch(error => {
                        podListDiv.innerHTML = '<p style="color: red;">Error loading pods: ' + error + '</p>';
                    });
            }

            function backToNamespaces() {
                podListDiv.style.display = 'none';
                namespaceListDiv.style.display = 'block';
            }

            function selectPod(ns, name) {
                fetch(`/select_pod/${ns}/${name}`)
                    .then(() => {
                        evtSource.close();
                        logsDiv.innerHTML = '<p>Switching to ' + name + ' in ' + ns + '</p>';
                        evtSource = new EventSource("/logs");
                    });
            }

            // Load namespaces on page load
            loadNamespaces();
        </script>
    </body>
    </html>
    """)

def run_flask():
    """Run Flask in a background thread."""
    app.run(debug=False, use_reloader=False, port=5000, host='0.0.0.0')

def load_kubeconfig(kubeconfig_path: str = "/kubeconfig.yaml") -> bool:  # Adjusted for Docker mount
    """Load and test the Kubernetes configuration."""
    if not os.path.exists(kubeconfig_path):
        print(f"{colorama.Fore.RED}Error: Kubeconfig file '{kubeconfig_path}' not found.")
        return False
    try:
        config.load_kube_config(config_file=kubeconfig_path)
        client.CoreV1Api().list_namespace()  # Test connectivity
        print(f"{colorama.Fore.GREEN}Kubeconfig loaded and tested successfully.")
        return True
    except Exception as e:
        print(f"{colorama.Fore.RED}Error loading or testing kubeconfig: {e}")
        return False

def list_pods(namespace: str = None) -> List[dict]:
    """List pods in the given namespace or all allowed namespaces."""
    v1 = client.CoreV1Api()
    try:
        if namespace:
            if namespace not in ALLOWED_NAMESPACES:
                print(f"{colorama.Fore.RED}Namespace '{namespace}' not allowed.")
                return []
            pods = v1.list_namespaced_pod(namespace).items
        else:
            pods = v1.list_pod_for_all_namespaces().items
            pods = [pod for pod in pods if pod.metadata.namespace in ALLOWED_NAMESPACES]
        if not pods:
            return []
        return [{"index": i, "name": pod.metadata.name, "namespace": pod.metadata.namespace} for i, pod in enumerate(pods, start=1)]
    except ApiException as e:
        print(f"{colorama.Fore.RED}Error listing pods: {e}")
        return []

def display_dashboard(pods: List[dict], current_namespace: str) -> None:
    """Display the main dashboard."""
    os.system('cls' if os.name == 'nt' else 'clear')
    display_ns = current_namespace or 'Allowed Namespaces'
    print(f"{colorama.Fore.CYAN}=== Kubernetes Pod Log Viewer Dashboard (Namespace: {display_ns}) ===")
    if not pods:
        print(f"{colorama.Fore.YELLOW}No pods available.")
    else:
        for pod in pods:
            print(f"{colorama.Fore.GREEN}{pod['index']}: {pod['name']} (Namespace: {pod['namespace']})")
    print(f"{colorama.Fore.CYAN}Enter pod ID to view logs in web and terminal, namespace name to filter (or 'all'/'q' to quit): ", end="")

def start_log_streaming(pod_name: str, namespace: str) -> None:
    """Start streaming logs from the pod into the queue for Flask and print to terminal, cache in Redis, with reconnection."""
    global current_pod, stream_thread
    if namespace not in ALLOWED_NAMESPACES:
        print(f"{colorama.Fore.RED}Namespace '{namespace}' not allowed.")
        return
    current_pod = {'name': pod_name, 'namespace': namespace}
    stop_event.clear()  # Reset stop event
    
    # Clear the queue to avoid old logs
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break
    
    cache_key = f"logs:{namespace}:{pod_name}"
    redis_client.delete(cache_key)  # Clear old cache
    
    def streamer():
        v1 = client.CoreV1Api()
        last_timestamp = None
        w = None
        while not stop_event.is_set():
            try:
                kwargs = {
                    'name': pod_name,
                    'namespace': namespace,
                    'follow': True,
                    'timestamps': True,  # Include timestamps to resume
                    'tail_lines': 100 if last_timestamp is None else 0,
                }
                if last_timestamp:
                    kwargs['since_time'] = last_timestamp
                w = watch.Watch()
                for line in w.stream(v1.read_namespaced_pod_log, **kwargs):
                    if stop_event.is_set():
                        break
                    # Parse timestamp and message
                    parts = line.split(' ', 1)
                    if len(parts) == 2:
                        last_timestamp = parts[0]
                        msg = parts[1]
                    else:
                        msg = line
                    print(msg)  # Print to terminal
                    log_queue.put(msg)  # Put log line into queue for Flask
                    redis_client.rpush(cache_key, msg)  # Cache in Redis
                    if redis_client.llen(cache_key) > 1000:
                        redis_client.lpop(cache_key)  # Trim to 1000 lines
                # If exited without stop, reconnect
            except ApiException as e:
                error_msg = f"Error streaming logs: {e}"
                print(f"{colorama.Fore.RED}{error_msg}")
                log_queue.put(error_msg)
                redis_client.rpush(cache_key, error_msg)
                time.sleep(1)  # Retry delay
            finally:
                if w is not None:
                    w.stop()
        log_queue.put('__END__')  # Sentinel to end SSE stream
        current_pod = None

    stream_thread = threading.Thread(target=streamer)
    stream_thread.start()

def stop_log_streaming() -> None:
    """Stop the current log streaming if active."""
    global stream_thread
    stop_event.set()
    if stream_thread and stream_thread.is_alive():
        stream_thread.join(timeout=2.0)
        stream_thread = None

def stream_pod_logs(pod_name: str, namespace: str) -> None:
    """Manage log streaming with user input handling."""
    if namespace not in ALLOWED_NAMESPACES:
        print(f"{colorama.Fore.RED}Namespace '{namespace}' not allowed.")
        return
    print(f"\n{colorama.Fore.CYAN}Streaming logs for pod '{pod_name}' in '{namespace}' to web and terminal (including last 100 lines; Type 'q' and press Enter to return; Ctrl+C also works)...")

    stop_log_streaming()  # Stop any existing stream
    start_log_streaming(pod_name, namespace)

    # Input reader thread
    def input_reader():
        try:
            while not stop_event.is_set():
                line = input()
                input_queue.put(line.strip().lower())
        except EOFError:
            pass

    input_thread = threading.Thread(target=input_reader)
    input_thread.start()

    try:
        while stream_thread and stream_thread.is_alive() and not stop_event.is_set():
            try:
                user_input = input_queue.get(timeout=0.5)
                if user_input == 'q':
                    print(f"\n{colorama.Fore.YELLOW}Exiting logs (user requested). Returning to dashboard...")
                    break
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        print(f"\n{colorama.Fore.YELLOW}Logs interrupted (Ctrl+C). Returning to dashboard...")
    finally:
        stop_event.set()  # Ensure input reader stops
        stop_log_streaming()
        input_thread.join(timeout=1.0)

def main(initial_namespace: str) -> None:
    """Main loop."""
    if not load_kubeconfig():
        sys.exit(1)
    
    # Start Flask in background
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"{colorama.Fore.GREEN}Flask web server started. Visit http://localhost:5000 for pod list and logs.")

    if sys.stdin.isatty():
        # Interactive mode: Run CLI dashboard
        current_namespace = initial_namespace if initial_namespace in ALLOWED_NAMESPACES else None
        while True:
            pods = list_pods(current_namespace)
            display_dashboard(pods, current_namespace)
            try:
                choice = input().strip().lower()
            except EOFError:
                print(f"{colorama.Fore.RED}Input error. Exiting.")
                break
            if choice == 'q':
                print(f"{colorama.Fore.GREEN}Exiting program.")
                sys.exit(0)
            elif choice == 'all':
                current_namespace = None
                continue
            elif choice:  # Assume it's a namespace name or pod ID
                try:
                    pod_index = int(choice) - 1
                    if 0 <= pod_index < len(pods):
                        selected_pod = pods[pod_index]
                        print(f"{colorama.Fore.CYAN}Logs are now streaming at http://localhost:5000. Open this URL in your browser.")
                        stream_pod_logs(selected_pod["name"], selected_pod["namespace"])
                    else:
                        raise ValueError
                except ValueError:
                    # Treat as namespace filter
                    if choice in ALLOWED_NAMESPACES:
                        current_namespace = choice
                        print(f"{colorama.Fore.YELLOW}Switching to namespace '{choice}'...")
                    else:
                        print(f"{colorama.Fore.RED}Namespace '{choice}' not allowed. Press Enter...")
                        input()
            else:
                print(f"{colorama.Fore.RED}Invalid input. Press Enter...")
                input()
    else:
        # Non-interactive mode (e.g., Docker): Just keep running
        print(f"{colorama.Fore.GREEN}Running in non-interactive mode. Use the web interface at http://localhost:5000")
        while True:
            time.sleep(1)  # Keep the container alive

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kubernetes Pod Log Viewer")
    parser.add_argument("--namespace", default="bizagi", help="Starting namespace (default: bizagi; must be allowed)")
    args = parser.parse_args()
    signal.signal(signal.SIGINT, lambda sig, frame: sys.exit(0))
    main(args.namespace)
