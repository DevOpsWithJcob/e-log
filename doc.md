Below, I'll provide a complete, step-by-step guide starting from the base (scratch) to set up your `log-viewer` service account with cluster-wide permissions to read pods and logs from **all namespaces**. This is based on the details in your query and logs:

- You've created the ServiceAccount in the `bizagi` namespace (not `default`), but your ClusterRoleBinding is still bound to `default`—this mismatch is causing the "forbidden" errors (the permissions aren't applied to the correct ServiceAccount).
- Your Kubernetes API server uses `http://192.168.10.36:6443` (insecure, no HTTPS). Normally, Kubernetes requires HTTPS for security, but if your cluster is configured this way (e.g., for testing or a custom setup), we'll handle it by setting `insecure-skip-tls-verify: true` in the kubeconfig. This skips TLS verification.
  - **Should you take the CRT (certificate-authority-data)?** No, you should **not** include it if using HTTP (insecure). The CA cert is for verifying HTTPS connections. If you include it with HTTP, it will cause errors (e.g., TLS handshake failures). Instead, we'll remove it and use `insecure-skip-tls-verify: true` to allow insecure connections. **Warning**: This is insecure and not recommended for production—expose the API over HTTPS if possible.
- We'll delete all existing related resources first to start fresh (avoiding conflicts).
- Assumptions:
  - You have admin-level access to the cluster via your default `kubectl` (without `--kubeconfig` flag) to run these commands.
  - The `bizagi` namespace already exists (based on your logs). If not, create it with `kubectl create namespace bizagi`.
  - We'll create the ServiceAccount in `bizagi` (as per your logs), but give it cluster-wide access.
  - All commands are run from your `~/service-account` directory.
- After setup, test with your existing Python script.

If any step fails, share the exact error output for troubleshooting.

### Step 1: Delete All Existing Resources (Start Fresh)
This removes the old ServiceAccount, ClusterRole, ClusterRoleBinding, and any leftover files/rolebindings. Run these one by one:

```
kubectl delete serviceaccount log-viewer --namespace bizagi  # Deletes the SA in bizagi (ignores if not found)
kubectl delete clusterrole pod-log-reader-cluster  # Deletes the cluster-wide role
kubectl delete clusterrolebinding pod-log-reader-cluster-binding  # Deletes the binding
kubectl delete role pod-log-reader --namespace default  # Just in case (ignores if not found)
kubectl delete rolebinding pod-log-reader-binding --namespace default  # Just in case (ignores if not found)
kubectl delete role pod-log-reader --namespace bizagi  # Just in case (ignores if not found)
kubectl delete rolebinding pod-log-reader-binding --namespace bizagi  # Just in case (ignores if not found)
rm -f log-viwer-sa-bizagi.yaml  # Delete old YAML file if it exists
rm -f kubeconfig.yaml  # Delete old kubeconfig to start fresh
```

- Expected output: Some may say "not found" (that's fine). This ensures a clean slate.

### Step 2: Create the YAML for Cluster-Wide RBAC
Create a new file called `log-viewer-sa.yaml` in `~/service-account` using a text editor (e.g., `vim log-viewer-sa.yaml`). Copy-paste the exact content below and save it.

This YAML:
- Creates the ServiceAccount in the `bizagi` namespace.
- Creates a ClusterRole with `get`, `list`, `watch` permissions on `pods` and `pods/log` (cluster-wide, for all namespaces).
- Creates a ClusterRoleBinding that binds the role to the ServiceAccount in `bizagi` (this fixes your "forbidden" issue).

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: log-viewer
  namespace: bizagi  # SA in bizagi namespace
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: pod-log-reader-cluster
rules:
- apiGroups: [""]
  resources: ["pods", "pods/log"]
  verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: pod-log-reader-cluster-binding
subjects:
- kind: ServiceAccount
  name: log-viewer
  namespace: bizagi  # Bind to bizagi namespace (fixes your mismatch)
roleRef:
  kind: ClusterRole
  name: pod-log-reader-cluster
  apiGroup: rbac.authorization.k8s.io
```

- Verify the file: `cat log-viewer-sa.yaml` (ensure no typos, especially indentation—use 2 spaces).

### Step 3: Apply the YAML to Create Resources
Run:
```
kubectl apply -f log-viewer-sa.yaml
```

- Expected output:
  - serviceaccount/log-viewer created
  - clusterrole.rbac.authorization.k8s.io/pod-log-reader-cluster created
  - clusterrolebinding.rbac.authorization.k8s.io/pod-log-reader-cluster-binding created
- If it says "unchanged," that's fine (but we deleted everything, so it should create fresh).

### Step 4: Generate a New Token for the ServiceAccount
Run:
```
kubectl create token log-viewer --namespace bizagi --duration=8760h  # 1-year duration; adjust if needed
```

- Expected output: A long token string (e.g., starting with `eyJhbGciOiJSUzI1NiIs...`). Copy this entire string exactly (no extra spaces or line breaks). We'll use it in the next step.
- If it fails with "serviceaccount not found," re-run Step 3.

### Step 5: Create the Kubeconfig File
Now create a fresh `kubeconfig.yaml` in `~/service-account` using a text editor (e.g., `vim kubeconfig.yaml`). Copy-paste the exact content below and save it.

Key changes:
- `server: http://192.168.10.36:6443` (using HTTP as per your setup—confirm this IP/port is correct and reachable from your machine).
- Added `insecure-skip-tls-verify: true` (required for HTTP/insecure access; skips any TLS checks).
- Removed `certificate-authority-data` entirely (not needed for insecure HTTP; including it would cause errors).
- Removed `namespace: default` from the context (since access is cluster-wide, it's optional—Kubernetes will default to "default" if needed, but you can query any namespace).
- Paste your **new token** from Step 4 into the `token` field (ensure it's one continuous line, no breaks).

```yaml
apiVersion: v1
clusters:
- cluster:
    insecure-skip-tls-verify: true  # Required for insecure HTTP server
    server: http://192.168.10.36:6443  # Your insecure API server URL - confirm reachable
  name: my-cluster
contexts:
- context:
    cluster: my-cluster
    user: log-viewer
    # namespace: default  # Removed - optional for cluster-wide access
  name: my-context
current-context: my-context
kind: Config
preferences: {}
users:
- name: log-viewer
  user:
    token: <paste-your-new-token-from-Step-4-here>  # Ensure no line breaks or extra spaces
```

- Verify the file: `cat kubeconfig.yaml` (check for correct token and no extra spaces).

### Step 6: Test the Setup
Test with `kubectl` using the new kubeconfig. These should all succeed now (no "forbidden" errors, thanks to the fixed binding).

1. **Test Cluster-Wide Pod Listing** (all namespaces):
   ```
   kubectl --kubeconfig=kubeconfig.yaml get pods --all-namespaces
   ```
   - Expected: Lists pods from all namespaces (or "No resources found" if none exist). If it fails with connection errors, check if `http://192.168.10.36:6443` is reachable (e.g., `curl http://192.168.10.36:6443/version` should return JSON with cluster info).

2. **Test Pod Listing in Specific Namespaces**:
   ```
   kubectl --kubeconfig=kubeconfig.yaml get pods -n default
   kubectl --kubeconfig=kubeconfig.yaml get pods -n bizagi
   kubectl --kubeconfig=kubeconfig.yaml get pods -n kube-system  # Example other namespace
   ```
   - Expected: Lists pods in each namespace without "forbidden."

3. **Test Log Access** (replace `my-pod-name` and `my-namespace` with real values from the listing above):
   ```
   kubectl --kubeconfig=kubeconfig.yaml logs my-pod-name -n my-namespace
   ```
   - Expected: Shows logs. If no pods, create a test one (using your admin kubectl): `kubectl run test-pod --image=busybox -n bizagi --command -- sh -c "while true; do echo 'Test log'; sleep 1; done"`.

4. **Test Permissions Explicitly**:
   ```
   kubectl --kubeconfig=kubeconfig.yaml auth can-i get pods --all-namespaces
   kubectl --kubeconfig=kubeconfig.yaml auth can-i get pods/log --all-namespaces
   ```
   - Expected: "yes" for both.

- **Common Fixes if Tests Fail**:
  - **Forbidden Errors**: Double-check the ClusterRoleBinding in Step 2/3 uses `namespace: bizagi`. Verify with `kubectl get clusterrolebinding pod-log-reader-cluster-binding -o yaml` (subjects should show `namespace: bizagi`).
  - **Connection Refused/Timeout**: Ensure the IP/port is correct and reachable (e.g., firewall open, same network). Test with `curl http://192.168.10.36:6443/healthz` (should return "ok").
  - **Invalid Token**: Regenerate in Step 4 and update kubeconfig.
  - **x509/TLS Errors**: If any appear, confirm `insecure-skip-tls-verify: true` is set and no `certificate-authority-data` is present.
  - If HTTP doesn't work, your cluster might still require HTTPS—check with `kubectl cluster-info` (using your admin access) to confirm the URL.

### Step 7: Run Your Python Program
Once tests pass:
- Ensure your `pod_log_viewer.py` is in `~/service-account`.
- Run: `python pod_log_viewer.py` (or with `--namespace bizagi` to start filtered).
- It should now list pods from all namespaces and allow log viewing without permission errors.

This completes the setup from scratch. If you need to switch the SA to `default` namespace instead, replace `bizagi` with `default` in Steps 2-4. Let me know the results of the tests!