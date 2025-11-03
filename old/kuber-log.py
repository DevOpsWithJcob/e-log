import os
import sys
import time
import signal
import argparse
from typing import List
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
import colorama  # pip install colorama
import threading  # Added for threaded log streaming

colorama.init(autoreset=True)

def load_kubeconfig(kubeconfig_path: str = "kubeconfig.yaml") -> bool:
    """Load and test the Kubernetes configuration."""
    if not os.path.exists(kubeconfig_path):
        print(f"{colorama.Fore.RED}Error: Kubeconfig file '{kubeconfig_path}' not found.")
        return False
    try:
        config.load_kube_config(config_file=kubeconfig_path)
        # Test connectivity with a simple API call
        client.CoreV1Api().list_namespace()  # This should succeed if config is valid
        print(f"{colorama.Fore.GREEN}Kubeconfig loaded and tested successfully.")
        return True
    except Exception as e:
        print(f"{colorama.Fore.RED}Error loading or testing kubeconfig: {e}")
        print(f"{colorama.Fore.YELLOW}Check API server URL, token, CA cert, or network connectivity.")
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
        print(f"{colorama.Fore.YELLOW}Possible issues: Invalid token, RBAC permissions, or wrong namespace.")
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
    print(f"{colorama.Fore.CYAN}Enter pod ID to view logs, namespace name to filter (or 'all'/'q' to quit): ", end="")

def stream_pod_logs(pod_name: str, namespace: str) -> None:
    """Stream logs from the pod in real-time."""
    v1 = client.CoreV1Api()
    w = watch.Watch()
    print(f"\n{colorama.Fore.CYAN}Streaming logs for pod '{pod_name}' in '{namespace}' (Type 'q' and press Enter to return to dashboard; Ctrl+C also works)...")

    def log_streamer():
        try:
            for line in w.stream(v1.read_namespaced_pod_log, name=pod_name, namespace=namespace, follow=True):
                print(line)
        except ApiException as e:
            print(f"{colorama.Fore.RED}Error streaming logs: {e}")
        finally:
            w.stop()

    # Start streaming in a background thread
    stream_thread = threading.Thread(target=log_streamer)
    stream_thread.start()

    # Main thread: Wait for user input to exit
    try:
        while stream_thread.is_alive():
            user_input = input().strip().lower()
            if user_input == 'q':
                print(f"\n{colorama.Fore.YELLOW}Exiting logs (user requested). Returning to dashboard...")
                break
    except KeyboardInterrupt:
        print(f"\n{colorama.Fore.YELLOW}Logs interrupted (Ctrl+C). Returning to dashboard...")
    finally:
        # Ensure the stream stops
        w.stop()
        stream_thread.join(timeout=1.0)  # Wait briefly for thread to finish

def main(initial_namespace: str) -> None:
    """Main loop."""
    if not load_kubeconfig():
        sys.exit(1)
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
