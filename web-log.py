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

# Flask imports (pip install flask)
from flask import Flask, Response, jsonify, request

colorama.init(autoreset=True)

# Global variables for sharing state between CLI and Flask
current_pod = None  # Will hold {'name': str, 'namespace': str}
log_queue = queue.Queue()  # Queue to hold log lines for Flask
input_queue = queue.Queue()  # Queue for user input in CLI
stop_event = threading.Event()  # To signal stopping the log stream
stream_thread = None  # Global stream thread

# Flask app setup
app = Flask(__name__)

@app.route('/logs')
def logs():
    """SSE endpoint for streaming logs."""
    def generate():
        yield 'data: {"message": "Connected to log stream"}\n\n'
        last_yield_time = time.time()
        has_yielded_waiting = False
        while not stop_event.is_set():
            try:
                line = log_queue.get(timeout=0.1)  # Shorter timeout for responsiveness
                if line == '__END__':  # Sentinel to end stream
                    break
                # Yield as JSON to avoid parse errors in JS
                yield f'data: {json.dumps({"log": line})}\n\n'
                last_yield_time = time.time()
                has_yielded_waiting = False  # Reset if we got a log
            except queue.Empty:
                current_time = time.time()
                if current_pod is None:
                    yield f'data: {json.dumps({"message": "No pod selected. Select a pod in the terminal or via web."})}\n\n'
                    last_yield_time = current_time
                elif not has_yielded_waiting:
                    yield f'data: {json.dumps({"message": "Waiting for logs..."})}\n\n'
                    last_yield_time = current_time
                    has_yielded_waiting = True  # Only yield waiting once
                elif current_time - last_yield_time > 15:  # Heartbeat every 15 seconds to keep connection alive
                    yield ': heartbeat\n\n'
                    last_yield_time = current_time
                time.sleep(0.1)  # Short sleep to avoid CPU spin
        yield f'data: {json.dumps({"message": "Log stream ended. Select another pod in terminal or web."})}\n\n'
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/pods')
def get_pods():
    """API to get list of all pods."""
    pods = list_pods(None)  # List all pods
    pod_list = [{"name": p["name"], "namespace": p["namespace"]} for p in pods]
    return jsonify(pod_list)

@app.route('/select_pod/<namespace>/<pod_name>')
def select_pod(namespace, pod_name):
    """Select a pod and start streaming its logs."""
    print(f"{colorama.Fore.YELLOW}Pod selected from web: {pod_name} in {namespace}")
    stop_log_streaming()
    start_log_streaming(pod_name, namespace)
    return "OK"

@app.route('/')
def index():
    """Simple HTML page to display pod list and logs."""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Kubernetes Pod Logs</title>
        <style>
            body { font-family: monospace; background: #f0f0f0; padding: 20px; }
            #pod-list { border: 1px solid #ccc; padding: 10px; margin-bottom: 20px; background: white; }
            #logs { border: 1px solid #ccc; padding: 10px; height: 400px; overflow-y: scroll; background: white; }
        </style>
    </head>
    <body>
        <h1>Real-Time Pod Logs</h1>
        <div id="pod-list">Loading pods...</div>
        <button onclick="loadPods()">Refresh Pods</button>
        <div id="logs">Connecting to stream...</div>
        <script>
            const logsDiv = document.getElementById('logs');
            const podListDiv = document.getElementById('pod-list');
            const evtSource = new EventSource("/logs");

            evtSource.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    if (data.message) {
                        logsDiv.innerHTML += '<p style="color: gray;">' + data.message + '</p>';
                    } else if (data.log) {
                        logsDiv.innerHTML += '<p>' + data.log.replace(/\\n/g, '<br>') + '</p>';  // Handle newlines
                    } else {
                        logsDiv.innerHTML += '<p>' + event.data + '</p>';  // Fallback
                    }
                } catch (e) {
                    logsDiv.innerHTML += '<p style="color: red;">Error parsing log: ' + event.data + '</p>';
                }
                logsDiv.scrollTop = logsDiv.scrollHeight;  // Auto-scroll
            };
            evtSource.onerror = function() {
                logsDiv.innerHTML += '<p style="color: red;">Connection error. Refresh the page.</p>';
            };

            function loadPods() {
                fetch('/pods')
                    .then(response => response.json())
                    .then(pods => {
                        let html = '<h2>Available Pods (Click to select)</h2><ul>';
                        pods.forEach(pod => {
                            html += `<li><a href="#" onclick="selectPod('${pod.namespace}', '${pod.name}')">${pod.name} (${pod.namespace})</a></li>`;
                        });
                        html += '</ul>';
                        podListDiv.innerHTML = html;
                    })
                    .catch(error => {
                        podListDiv.innerHTML = '<p style="color: red;">Error loading pods: ' + error + '</p>';
                    });
            }

            function selectPod(ns, name) {
                fetch(`/select_pod/${ns}/${name}`)
                    .then(() => {
                        logsDiv.innerHTML = '<p>Switching to ' + name + ' in ' + ns + '</p>';
                    });
            }

            // Load pods on page load
            loadPods();
        </script>
    </body>
    </html>
    """

def run_flask():
    """Run Flask in a background thread."""
    app.run(debug=False, use_reloader=False, port=5000)

def load_kubeconfig(kubeconfig_path: str = "kubeconfig.yaml") -> bool:
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
    """List pods in the given namespace or all namespaces."""
    v1 = client.CoreV1Api()
    try:
        if namespace:
            print(f"{colorama.Fore.YELLOW}Listing pods in namespace '{namespace}'...")
            pods = v1.list_namespaced_pod(namespace).items
        else:
            print(f"{colorama.Fore.YELLOW}Listing pods in all namespaces...")
            pods = v1.list_pod_for_all_namespaces().items
        if not pods:
            print(f"{colorama.Fore.YELLOW}No pods found.")
        return [{"index": i, "name": pod.metadata.name, "namespace": pod.metadata.namespace} for i, pod in enumerate(pods, start=1)]
    except ApiException as e:
        print(f"{colorama.Fore.RED}Error listing pods: {e}")
        return []

def display_dashboard(pods: List[dict], current_namespace: str) -> None:
    """Display the main dashboard."""
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"{colorama.Fore.CYAN}=== Kubernetes Pod Log Viewer Dashboard (Namespace: {current_namespace or 'All'}) ===")
    if not pods:
        print(f"{colorama.Fore.YELLOW}No pods available.")
    else:
        for pod in pods:
            print(f"{colorama.Fore.GREEN}{pod['index']}: {pod['name']} (Namespace: {pod['namespace']})")
    print(f"{colorama.Fore.CYAN}Enter pod ID to view logs in web and terminal, namespace name to filter (or 'all'/'q' to quit): ", end="")

def start_log_streaming(pod_name: str, namespace: str) -> None:
    """Start streaming logs from the pod into the queue for Flask and print to terminal."""
    global current_pod, stream_thread
    current_pod = {'name': pod_name, 'namespace': namespace}
    stop_event.clear()  # Reset stop event
    
    # Clear the queue to avoid old logs
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break
    
    def streamer():
        v1 = client.CoreV1Api()
        w = watch.Watch()
        try:
            for line in w.stream(v1.read_namespaced_pod_log, name=pod_name, namespace=namespace, follow=True, tail_lines=100):
                if stop_event.is_set():
                    break
                print(line)  # Print to terminal
                log_queue.put(line)  # Put log line into queue for Flask
        except ApiException as e:
            error_msg = f"Error streaming logs: {e}"
            print(f"{colorama.Fore.RED}{error_msg}")
            log_queue.put(error_msg)
        finally:
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

    current_namespace = initial_namespace if initial_namespace != "all" else None
    while True:
        pods = list_pods(current_namespace)
        display_dashboard(pods, current_namespace)
        choice = input().strip().lower()
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
                current_namespace = choice
                print(f"{colorama.Fore.YELLOW}Switching to namespace '{choice}'...")
        else:
            print(f"{colorama.Fore.RED}Invalid input. Press Enter...")
            input()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kubernetes Pod Log Viewer")
    parser.add_argument("--namespace", default="bizagi", help="Starting namespace (default: all; use 'default' for specific)")
    args = parser.parse_args()
    signal.signal(signal.SIGINT, lambda sig, frame: sys.exit(0))
    main(args.namespace)
