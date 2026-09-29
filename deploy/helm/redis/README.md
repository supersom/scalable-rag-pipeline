# Redis on EKS

`deploy/helm/redis/values.yaml` pins Bitnami Redis chart 28.3.0 to one primary and one read replica. Each pod has an 8 GiB EBS claim in the `gp2` storage class. Kubernetes spreads the pods across availability zones when both zones have schedulable nodes. The current cluster has only two system nodes, so the pods tolerate their `CriticalAddonsOnly` taint. Redis is password protected; the Helm release stores the generated password in the Kubernetes `redis` Secret.

Deploy or update with `bash scripts/bootstrap_cluster.sh --start 15`. The chart exposes `redis-master.default.svc.cluster.local:6379` for writes and `redis-replicas.default.svc.cluster.local:6379` for reads. Check placement and health with `kubectl get pods -l app.kubernetes.io/instance=redis -o wide` and `kubectl get pvc -l app.kubernetes.io/instance=redis`.

The API currently uses Qdrant for its semantic cache and does not connect to this Redis release. This primary/replica setup does not include Sentinel or automatic promotion. If the primary fails, writes remain unavailable until an operator promotes the replica and redirects clients. EBS volumes stay bound to their availability zones.
