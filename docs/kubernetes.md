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

./scripts/generate-secrets.sh
```

This is the same script compose uses. It writes seven files to `secrets/`
(and a `.env` that Kubernetes ignores). Load them into a Secret — the file
names become the keys the manifests expect:

```bash
kubectl create namespace powerdns
kubectl -n powerdns create secret generic powerdns-secrets --from-file=secrets/
```

::: tip Bring your own secret store
Nothing in the manifests cares how `powerdns-secrets` came to exist. With
External Secrets, Sealed Secrets or Vault, create a Secret of that name with
the keys `db_superuser_password`, `pdns_db_password`, `webui_db_password`,
`pdns_api_key`, `recursor_api_key`, `webui_secret_key` and
`webui_admin_password`. Both API keys must be at least 16 characters.
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

Open <http://localhost:9191> and sign in as `admin` with the password in
`secrets/webui_admin_password`. **Change it under *My profile* straight away.**

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
value to `powerdns-secrets` and point at it:

```bash
kubectl -n powerdns create secret generic powerdns-secrets --from-file=secrets/ \
  --from-literal=ldap_bind_password='…' --dry-run=client -o yaml | kubectl apply -f -
```

Then in `webui.yaml`, add the key to the `secrets` volume's `items` and the
variable to the container's `env`:

```yaml
- name: LDAP_BIND_PASSWORD_FILE
  value: /run/secrets/ldap_bind_password
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
from `secrets/` as in [step 1](#_1-generate-the-secrets). The pods start on
their own once it exists.

**`recursor-dns` stays `<pending>`.** The cluster has no LoadBalancer
implementation; see [No LoadBalancer](#no-loadbalancer).

**Your own zones return `SERVFAIL` through the recursor.** Look at the
Forwarding page: it lists the rules the panel keeps for your zones and the
address they point at, which must equal `PDNS_DNS_ADDRESS`.
