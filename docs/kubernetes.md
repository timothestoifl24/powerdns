---
title: Kubernetes
description: Running the stack on Kubernetes — the manifests in deploy/kubernetes, secrets, the fixed address the recursor needs, exposing DNS on port 53 and the panel behind an Ingress.
---

# Kubernetes

The same four images that `compose.yml` runs can be deployed to any
Kubernetes cluster with the manifests in
[`deploy/kubernetes`](https://github.com/timothestoifl24/powerdns/tree/main/deploy/kubernetes).
They are plain YAML assembled with [kustomize](https://kustomize.io), which is
built into `kubectl` — no Helm, no operator, nothing to install.

```
                   clients :53 tcp/udp
                          │
             Service recursor-dns (LoadBalancer)
                          │
┌──────────────┐   ┌──────▼───────┐  forwards local zones  ┌──────────────┐
│ Deployment   │   │ Deployment   │ ─────────────────────► │ Deployment   │
│ webui        │──►│ recursor     │   to a fixed ClusterIP │ pdns         │
│ Svc webui:80 │   │ PVC api zones│                        │ Svc pdns     │
└──────┬───────┘   └──────────────┘                        └──────┬───────┘
       │                 HTTP API :8081                           │
       └──────────────────────────────────────────────────────────┤
                                                                  ▼
                                                   ┌──────────────────────┐
                                                   │ StatefulSet db       │
                                                   │ PostgreSQL 18 + PVC  │
                                                   └──────────────────────┘
```

| Object | Kind | Notes |
| --- | --- | --- |
| `db` | StatefulSet + headless Service | PostgreSQL 18, 2 Gi `PersistentVolumeClaim` |
| `pdns` | Deployment + ClusterIP Service | Stateless; the Service has a **fixed** cluster IP |
| `recursor` | Deployment + ClusterIP Service | The API the panel drives, 64 Mi PVC for forward zones |
| `recursor-dns` | LoadBalancer Service | Port 53 TCP and UDP for clients |
| `webui` | Deployment + ClusterIP Service | The panel on port 80 |
| `powerdns-settings` | ConfigMap | Generated from `settings.env` — the counterpart of `.env` |
| `powerdns-secrets` | Secret | Created by you from `./secrets` |

## Requirements

- Kubernetes **1.26 or newer** — a LoadBalancer that carries TCP and UDP on
  the same port became stable in 1.26.
- `kubectl` 1.27+ (for `kubectl apply -k` with the kustomize features used).
- A default StorageClass, so the two PersistentVolumeClaims are bound. Check
  with `kubectl get storageclass`.
- For DNS from outside the cluster: a LoadBalancer implementation (every cloud
  has one; on bare metal use [MetalLB](https://metallb.io) or k3s's built-in
  ServiceLB), or one of the [alternatives](#no-loadbalancer) below.

The images are published multi-architecture (amd64 and arm64) to
`ghcr.io/timothestoifl24/`, so nothing has to be built.

## Install

### 1. Generate the secrets

```bash
git clone https://github.com/timothestoifl24/powerdns.git
cd powerdns

./scripts/k8s-secrets.sh create
```

This creates the `powerdns` namespace and the `powerdns-secrets` Secret with
seven random values, generated on the spot and handed to `kubectl` on stdin.
**No copy is written to disk**, none appears on a command line (and so in
shell history or `ps`), and the Secret is made with `kubectl create`, not
`apply`, which would store a second copy in its
`last-applied-configuration` annotation. The Secret is the only place the
values live; running `create` again leaves an existing one alone, since the
database roles keep the passwords they were created with.

Pass `-n <namespace>` to use another namespace, and set the same one in
`kustomization.yaml`.

::: warning Protect the Secret, not a file
Kubernetes stores Secrets base64-encoded, which is not encryption. Anyone who
can `get secrets` in the namespace can read every value, so keep that right
to the people who run the stack, and turn on
[encryption at rest](https://kubernetes.io/docs/tasks/administer-cluster/encrypt-data/)
where the cluster allows it.
:::

::: tip Bring your own secret store
Nothing in the manifests cares how `powerdns-secrets` came to exist. With
External Secrets, Sealed Secrets or Vault, create a Secret of that name with
the keys `db_superuser_password`, `pdns_db_password`, `webui_db_password`,
`pdns_api_key`, `recursor_api_key` and `webui_secret_key`, plus
`webui_admin_password` for the first start only. Both API keys must be at
least 16 characters.
:::

### 2. Pick the authoritative server's address

Open `deploy/kubernetes/settings.env` and check `PDNS_DNS_ADDRESS`. It must be
an **unused address inside your cluster's Service CIDR**. The default,
`10.96.0.53`, fits kubeadm, kind, Docker Desktop and most distributions that
keep the `10.96.0.0/12` default. k3s uses `10.43.0.0/16` — use `10.43.0.53`
there. On a managed cluster, ask the API server:

```bash
kubectl create service clusterip probe --tcp=53 --clusterip=1.1.1.1 --dry-run=server
# error: ... provided IP is not in the valid range. The range of valid IPs is 10.96.0.0/12
```

[Why a fixed address?](#why-the-pdns-service-has-a-fixed-address)

### 3. Apply

```bash
kubectl apply -k deploy/kubernetes
kubectl -n powerdns get pods -w
```

The first start takes a minute or two: the database volume is provisioned,
`initdb` runs the schema scripts, and `pdns` and `webui` wait for it. All four
pods should end up `Running` and `1/1` ready.

### 4. Sign in

```bash
kubectl -n powerdns port-forward svc/webui 9191:80
```

Read the first-run password straight from the Secret — the script prints it
to a terminal only, never into a pipe or a log:

```bash
./scripts/k8s-secrets.sh admin-password
```

Open <http://localhost:9191>, sign in as `admin` with it, and **change it
under *My profile* straight away.** Then delete it:

```bash
./scripts/k8s-secrets.sh forget-admin-password
```

The panel reads it only while its user table is empty, so once you have
signed in it is a live credential that no longer does anything — nothing
restarts, and the pod starts fine without it.

### 5. Check DNS

```bash
kubectl -n powerdns get svc recursor-dns
# NAME           TYPE           CLUSTER-IP     EXTERNAL-IP    PORT(S)
# recursor-dns   LoadBalancer   10.96.12.34    203.0.113.10   53:31053/UDP,53:31053/TCP

dig @203.0.113.10 example.com SOA
```

Create a zone in the panel and query it through the same address: the
recursor forwards every zone the panel creates to the authoritative server.

## Configuration

`settings.env` is the Kubernetes counterpart of `.env`. Every key in it is
turned into the `powerdns-settings` ConfigMap and handed to the `pdns`,
`recursor` and `webui` containers as environment variables, so everything in
`.env.example` and [Advanced configuration](/advanced-config) applies
unchanged — LDAP, OAuth, SAML, `PDNS_SETTING_*`, `RECURSOR_EXTRA_YAML` aside
(it is multi-line; see below).

After editing it, apply again:

```bash
kubectl apply -k deploy/kubernetes
```

The ConfigMap's name carries a hash of its contents, so a change rolls every
pod that reads it — no manual restart.

### Secrets for authentication providers

An LDAP bind password or an OAuth client secret belongs in the Secret, not in
`settings.env`. Every such setting also accepts a `_FILE` variant, so add the
value to `powerdns-secrets` and point at it. The script prompts for it without
echoing, or reads one line from stdin, so it stays out of your shell history:

```bash
./scripts/k8s-secrets.sh set ldap_bind_password
# or straight from a password manager
op read 'op://Infra/LDAP bind/password' | ./scripts/k8s-secrets.sh set ldap_bind_password
```

Then in `webui.yaml`, add the key to the `secrets` volume's `items` and the
variable to the container's `env`:

```yaml
- name: LDAP_BIND_PASSWORD_FILE
  value: /run/secrets/powerdns/ldap_bind_password
```

### Pinning a release

The manifests follow `latest`. To run a release, set the tag for all four
images in `kustomization.yaml`:

```yaml
images:
  - name: ghcr.io/timothestoifl24/pdns-db
    newTag: "1.2"
  # … and the same for pdns, pdns-recursor and pdns-webui
```

Releases publish both `1.2.3` and `1.2` tags; see [Upgrading](/upgrading)
before moving between them.

### Your own overlay

Rather than editing the files in place, keep your changes in an overlay that
pulls the base from this repository — then `git pull` never conflicts with
them:

```yaml
# my-dns/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - https://github.com/timothestoifl24/powerdns//deploy/kubernetes?ref=v1.2.0
configMapGenerator:
  - name: powerdns-settings
    behavior: merge
    literals:
      - PDNS_DNS_ADDRESS=10.43.0.53
      - DEFAULT_NAMESERVERS=ns1.corp.example,ns2.corp.example
      - BASE_URL=https://dns.corp.example
      - SESSION_COOKIE_SECURE=true
      - TRUSTED_PROXY_COUNT=1
# Repeat this whenever the overlay changes PDNS_DNS_ADDRESS: the base's copy
# runs before your merge, so without it the Service keeps the base's address.
replacements:
  - source:
      kind: ConfigMap
      name: powerdns-settings
      fieldPath: data.PDNS_DNS_ADDRESS
    targets:
      - select:
          kind: Service
          name: pdns
        fieldPaths:
          - spec.clusterIP
          - spec.clusterIPs.0
```

`RECURSOR_EXTRA_YAML` and anything else multi-line goes in the same way with a
strategic-merge patch on the `recursor` Deployment's `env`.

## Exposing the panel

The `webui` Service is ClusterIP only. For anything longer-lived than a
`port-forward`, put an Ingress in front of it — for example with ingress-nginx
and cert-manager:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: webui
  namespace: powerdns
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt
spec:
  ingressClassName: nginx
  tls:
    - hosts: [dns.example.com]
      secretName: webui-tls
  rules:
    - host: dns.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: webui
                port:
                  name: http
```

and set, in `settings.env`:

```bash
BASE_URL=https://dns.example.com
SESSION_COOKIE_SECURE=true     # the Ingress terminates TLS
TRUSTED_PROXY_COUNT=1          # the Ingress controller
```

The same rules as in [Behind a reverse proxy](/setup#behind-a-reverse-proxy)
apply: without `SESSION_COOKIE_SECURE=true` over HTTPS it works, but with it
over plain HTTP login silently fails.

## Exposing DNS

`recursor-dns` is a LoadBalancer with `externalTrafficPolicy: Local`, so the
recursor sees each client's real address and `RECURSOR_ALLOW_FROM` means what
it says. The default allows only private ranges — **never widen it to
`0.0.0.0/0`**: an open resolver is found within days and used to amplify
attacks on others. If your clients reach the load balancer from public
addresses, list exactly those networks.

Some providers want annotations to create an internal (private) load
balancer rather than a public one — for example
`service.beta.kubernetes.io/aws-load-balancer-scheme: internal` or
`networking.gke.io/load-balancer-type: Internal`. Add them with a patch on the
`recursor-dns` Service. MetalLB takes a fixed address through
`metallb.io/loadBalancerIPs: 192.0.2.53`.

### No LoadBalancer

On a cluster without one, any of these work:

- **NodePort.** Patch `recursor-dns` to `type: NodePort` and query any node on
  the allocated port (30000–32767). Fine for testing; clients rarely accept a
  DNS server on a non-standard port.
- **hostPort.** Add `hostPort: 53` to the recursor's two DNS ports, and it
  answers on port 53 of whichever node runs it. Pin it with a `nodeSelector`
  so the address stays the same.
- **From inside the cluster only.** Leave it as is — or switch it to
  ClusterIP — and point CoreDNS at it for your zones with a `forward` block in
  the `coredns` ConfigMap.

On [kind](https://kind.sigs.k8s.io) none of these reach the host by
themselves, because the node is a container; see
[Running on a single machine with kind](#running-on-a-single-machine-with-kind).

## Running on a single machine with kind

kind is a convenient way to run the stack on one box — a Raspberry Pi, a
home server — but its node is a container, so a NodePort opens inside that
container and not on the machine. `kubectl port-forward` is no way round it
for DNS either: it carries TCP only, and most DNS queries are UDP. The fix is
to have kind publish the NodePorts on the host, which it can only do when the
cluster is created.

Keep your changes next to the clone rather than in it, so `git pull` never
conflicts with them:

```
/opt/
├── powerdns/                   the git clone, untouched
└── powerdns-local/
    ├── kind.yaml               port mappings, read when the cluster is created
    └── kustomization.yaml      fixed NodePorts for DNS and the panel
```

`/opt/powerdns-local/kind.yaml` — the machine's port 53 (UDP and TCP) and
9191 go to fixed NodePorts inside the node. Replace `192.168.1.50` with the
machine's LAN address (`hostname -I`), and reserve that address in your
router's DHCP settings so it never moves under your clients:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - { containerPort: 30053, hostPort: 53, protocol: UDP, listenAddress: "192.168.1.50" }
      - { containerPort: 30053, hostPort: 53, protocol: TCP, listenAddress: "192.168.1.50" }
      - { containerPort: 30080, hostPort: 9191, protocol: TCP, listenAddress: "0.0.0.0" }
```

::: warning Name an address for port 53, never `0.0.0.0`
Podman resolves container names with aardvark-dns, which listens on port 53
of every Podman network's gateway — `10.89.0.1` for kind's network. Port 53 on
`0.0.0.0` claims that address too, and `kind create cluster` fails with
*aardvark-dns failed to start … failed to bind udp listener on 10.89.0.1:53:
Address already in use*. Binding the LAN address leaves the gateway to
aardvark-dns. It is the same collision compose runs into; see
[Podman: bind an address](/setup#podman-bind-an-address-never-0-0-0-0). The
panel's port has no such conflict. Docker is unaffected, but the named
address works there as well.
:::

`/opt/powerdns-local/kustomization.yaml` — the manifests from the clone, with
`recursor-dns` and `webui` turned into NodePorts on exactly those ports:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../powerdns/deploy/kubernetes
patches:
  - target:
      kind: Service
      name: recursor-dns
    patch: |-
      - op: replace
        path: /spec/type
        value: NodePort
      - op: add
        path: /spec/ports/0/nodePort
        value: 30053
      - op: add
        path: /spec/ports/1/nodePort
        value: 30053
  - target:
      kind: Service
      name: webui
    patch: |-
      - op: replace
        path: /spec/type
        value: NodePort
      - op: add
        path: /spec/ports/0/nodePort
        value: 30080
```

The patches address the ports by position on purpose. Both DNS ports are
port 53, one UDP and one TCP, and a strategic-merge patch matches Service
ports by number alone — it would fold the two into one.

Then create the cluster and deploy through the overlay rather than
`deploy/kubernetes`:

```bash
kind create cluster --config /opt/powerdns-local/kind.yaml
cd /opt/powerdns && ./scripts/k8s-secrets.sh create
kubectl apply -k /opt/powerdns-local
kubectl -n powerdns get pods -w          # until all four are 1/1 Running
```

The panel is at `http://<machine>:9191`, and DNS answers on port 53 of the
address you named — from the machine itself too, so query that address
rather than `127.0.0.1`. From another device:

```bash
dig @<machine> example.com SOA
dig @<machine> +tcp example.com SOA
```

Once both answer, point your router's DHCP DNS option at the machine.

### Moving an existing kind cluster over

Port mappings cannot be added to a running kind cluster, so an existing one
has to be recreated, and recreating it deletes the database volume. Dump the
zones first and restore them into the new cluster:

```bash
kubectl -n powerdns exec db-0 -- pg_dump -U postgres -Fc pdns > ~/pdns.dump

kind delete cluster
kind create cluster --config /opt/powerdns-local/kind.yaml
cd /opt/powerdns && ./scripts/k8s-secrets.sh create
kubectl apply -k /opt/powerdns-local
kubectl -n powerdns get pods -w          # wait for 1/1 Running

kubectl -n powerdns exec -i db-0 -- pg_restore -U postgres -d pdns --clean --if-exists < ~/pdns.dump
```

The new Secret has new database passwords, which is fine: they belong to the
new database, and the restore brings back the zones and the panel's users —
your admin password included — rather than the roles. Its first-run admin
password goes unused, because users already exist; remove it with
`./scripts/k8s-secrets.sh forget-admin-password`.

### Before you open port 53

- **Nothing else may hold it.** `sudo ss -lunp | grep ':53 '` shows who does;
  on many distributions it is `systemd-resolved` or `dnsmasq`, whose listener
  has to go first. See [Something already owns port 53](/setup#something-already-owns-port-53).
- **Rootless Podman cannot bind ports below 1024.** Run kind with rootful
  Podman or Docker, or allow it once:
  `sudo sysctl net.ipv4.ip_unprivileged_port_start=53` (persist it in
  `/etc/sysctl.d/`).
- **`RECURSOR_ALLOW_FROM` no longer tells clients apart.** Every query reaches
  the recursor through the container runtime's NAT, so it arrives from a
  private address and is allowed, whoever sent it. Restrict port 53 on the
  machine's firewall instead — for example
  `sudo ufw allow from 192.168.1.0/24 to any port 53` with your LAN's subnet.
- **Never forward port 53 from your router.** A recursive resolver reachable
  from the internet is found within days and used to amplify attacks on
  others.
- **The panel on 9191 is plain HTTP**, reachable by anyone on the network.
  Keep it on the LAN, or put it behind an [Ingress with TLS](#exposing-the-panel).

::: tip Just trying it out?
Without recreating anything, `kubectl port-forward` reaches the panel from
other machines if you ask it to listen on every address — by default it
binds `127.0.0.1` only:

```bash
nohup kubectl -n powerdns port-forward --address 0.0.0.0 svc/webui 9191:80 > /tmp/webui-pf.log 2>&1 &
```

It stops whenever the webui pod restarts or the machine reboots, and it
cannot carry DNS, so it is a stopgap rather than a setup.
:::

## Why the pdns Service has a fixed address

Forward targets in PowerDNS are IP addresses, never names: the recursor reads
them at configuration time, before it can resolve anything. So it cannot be
pointed at `pdns.powerdns.svc`, and the panel stores the address itself in
every forward rule it creates for your zones. Under compose the authoritative
container gets a fixed address on the compose network for this reason; on
Kubernetes it is the `pdns` Service's cluster IP.

A Service keeps its cluster IP for as long as it exists, so an ordinary
restart or rollout never changes it. Fixing it in the manifest is what makes
a deleted and re-created Service — or a fresh install restored from a backup
— come back on the same address. `kustomization.yaml` copies
`PDNS_DNS_ADDRESS` into the Service, so the panel and the Service cannot
disagree.

If you ever do change it, the forward rules pointing at the old address stay
behind, and the panel will not overwrite them (it never touches a rule that
points somewhere other than its own target). Delete them on the
**Forwarding** page and open it again; the panel re-creates them against the
new address.

## Pod networks and the API allow-lists

Both HTTP APIs accept requests only from the networks in
`PDNS_WEBSERVER_ALLOW_FROM` and `RECURSOR_WEBSERVER_ALLOW_FROM`, on top of the
API key. The defaults cover `10.0.0.0/8`, `172.16.0.0/12` and
`192.168.0.0/16`, which is where almost every CNI puts pods. If the panel
reports that the PowerDNS API refused it with *403*, your pod network is
elsewhere — some CNIs use `100.64.0.0/10`:

```bash
kubectl -n powerdns get pod -l app.kubernetes.io/name=webui -o wide   # the pod's IP
```

and add its range to both settings. A `NetworkPolicy` that admits only the
`webui` pods to ports 8081 and 8082 is a sensible addition on clusters that
enforce them.

## Scaling

- **pdns** is stateless and can run several replicas behind its Service.
  With more than one, set `PDNS_SETTING_zone_cache_refresh_interval` low, or a
  zone created through one replica stays invisible on the others for up to
  five minutes.
- **recursor** keeps its forward zones in a `ReadWriteOnce` volume and runs
  as one replica. The panel talks to it through one API, so a second replica
  would never be told about new forward zones.
- **webui** runs as one replica: its first start creates the schema, and the
  login lockout is counted per process.
- **db** is a single PostgreSQL instance. For high availability, point
  `PDNS_GPGSQL_HOST` and `DB_HOST` at an operator-managed cluster (CloudNativePG,
  Crunchy, a cloud database) instead, and create the two roles and schemas as
  `db/initdb/00-roles.sh` does.

## Backups and upgrades

Everything that matters is in PostgreSQL. Back it up the way compose users do,
through the pod:

```bash
kubectl -n powerdns exec db-0 -- pg_dump -U postgres -Fc pdns > pdns.dump
```

Upgrading is `kubectl apply -k` with the new tag. Read [Upgrading](/upgrading)
first: a major PostgreSQL version change needs a `pg_upgrade` step that no
rolling update can do for you.

## Removing it

```bash
kubectl delete -k deploy/kubernetes
kubectl -n powerdns delete pvc --all      # this deletes every zone
kubectl delete namespace powerdns
```

`kubectl delete -k` keeps the StatefulSet's volume claim, like
`docker compose down` keeps the `pgdata` volume; deleting the claims is the
`down -v`.

## Troubleshooting

**A pod is `Pending`.** Almost always an unbound PersistentVolumeClaim: there
is no default StorageClass. `kubectl -n powerdns describe pvc` says so.

**`The Service "pdns" is invalid: spec.clusterIPs: … not in the valid range`.**
`PDNS_DNS_ADDRESS` is outside your Service CIDR — see
[step 2](#_2-pick-the-authoritative-server-s-address).

**`… provided IP is already allocated`.** Another Service owns that address;
pick another.

**A pod is stuck in `ContainerCreating`.** `kubectl -n powerdns describe pod`
shows `secret "powerdns-secrets" not found` or a missing key: create the Secret
as in [step 1](#_1-generate-the-secrets). The pods start on
their own once it exists.

**`Deployment does not have minimum availability`.** The Deployment's pods are
not ready yet; `kubectl -n powerdns get pods` and `describe pod` say why. A
`RunContainerError` mentioning `mkdirat …/run/secrets/kubernetes.io:
read-only file system` means a secret volume was mounted at `/run/secrets`
itself: `/var/run` is `/run` in these images, so the service-account token
mount would have to be created inside it. The manifests mount the secrets at
`/run/secrets/powerdns` and turn the token off; keep it that way in any patch
of your own.

**`recursor-dns` stays `<pending>`.** The cluster has no LoadBalancer
implementation; see [No LoadBalancer](#no-loadbalancer).

**Your own zones return `SERVFAIL` through the recursor.** Look at the
Forwarding page: it lists the rules the panel keeps for your zones and the
address they point at, which must equal `PDNS_DNS_ADDRESS`.
