# Ijus: mini object storage for the ijads pink node

Ijus is a small S3-style file storage API built into `app1.py`, the single-file
Python server that also serves the **ijads pink node** status page. It needs no
framework: Python 3.9+ and `psutil` only.

- **Buckets** are folders inside `./storage/`.
- **Objects** are files stored directly inside a bucket folder.
- Every storage request needs a **Bearer token**.
- The server is multithreaded and dual-stack (IPv6 + IPv4) on ports **80** (HTTP) and **443** (HTTPS).

---

## Contents

1. [Quick start](#quick-start)
2. [Configuration](#configuration)
3. [Authentication](#authentication)
4. [API reference](#api-reference)
5. [Errors](#errors)
6. [Security](#security)
7. [Limits and behavior notes](#limits-and-behavior-notes)
8. [Testing](#testing)
9. [Running as a service](#running-as-a-service)
10. [Troubleshooting](#troubleshooting)

---

## Quick start

```bash
pip install psutil

export IJUS_TOKEN=choose-a-long-random-token
sudo -E "$(which python)" app1.py        # ports 80/443 need root (see Troubleshooting)
```

Then:

```bash
T="Authorization: Bearer $IJUS_TOKEN"

# upload (the bucket is created automatically)
curl -k -H "$T" -F "file=@photo.jpg" https://pink.ijadium.com/bucket/buckets/my-bucket/files

# list
curl -k -H "$T" https://pink.ijadium.com/bucket/buckets/my-bucket

# download
curl -k -H "$T" -O -J https://pink.ijadium.com/bucket/buckets/my-bucket/files/photo.jpg

# delete
curl -k -H "$T" -X DELETE https://pink.ijadium.com/bucket/buckets/my-bucket/files/photo.jpg
```

`-k` accepts the self-signed certificate. Drop it once you use a real certificate.

## Configuration

| Setting | How | Default |
|---|---|---|
| Auth token | env `IJUS_TOKEN` | `SUPER_SECRET_TOKEN_123` (a warning is printed) |
| Storage folder | env `IJUS_STORAGE` | `./storage` next to the script |
| API prefix | `PREFIX` constant in the script | `/bucket` |
| Max upload size | `MAX_UPLOAD` constant in the script | 2 GiB |
| TLS certificate | `cert.pem` + `key.pem` next to the script | auto-generated (self-signed, 825 days) with the `openssl` CLI if missing |

If no certificate can be loaded or generated, only HTTP on port 80 runs.

## Authentication

Every request under `/bucket/` must include:

```
Authorization: Bearer <IJUS_TOKEN>
```

A missing or wrong token returns `401 Unauthorized` with `WWW-Authenticate: Bearer`.
The token is compared in constant time (`hmac.compare_digest`).

The status page (`/`) and `/api/stats` are **public** and do not need a token.

> Use HTTPS when the server is reachable from the internet. Over plain HTTP the
> token and file contents travel unencrypted.

## API reference

Base URL: `https://pink.ijadium.com/bucket`

All responses are JSON except file downloads.

### List a bucket

```
GET /bucket/buckets/{bucket}
```

**200 OK**
```json
{
  "bucket": "my-bucket",
  "count": 2,
  "files": ["notes.txt", "photo.jpg"]
}
```

Files are sorted alphabetically. Sub-folders are ignored.
`404` if the bucket doesn't exist.

### Upload a file

```
POST /bucket/buckets/{bucket}/files
Content-Type: multipart/form-data
```

Send the file as a normal form file field (any field name works; the first part
that has a `filename` is saved). The bucket is created if it doesn't exist.

```bash
curl -H "$T" -F "file=@report.pdf" https://pink.ijadium.com/bucket/buckets/docs/files
```

**201 Created**
```json
{ "bucket": "docs", "file": "report.pdf", "size": 48213 }
```

- The stored name is the form's `filename`, reduced to its final path component
  (`../../x.txt` is saved as `x.txt`).
- Uploading an existing name **overwrites** it.
- The upload is written to a temporary file and swapped in atomically, so a failed
  upload never leaves a half-written file.
- Requires a `Content-Length` header (chunked transfer encoding is not supported).

### Download a file

```
GET /bucket/buckets/{bucket}/files/{filename}
```

**200 OK** streams the raw bytes with `Content-Type` guessed from the extension
(fallback `application/octet-stream`), `Content-Length`, and
`Content-Disposition: attachment; filename="..."`.
`404` if the bucket or file doesn't exist.

### Delete a file

```
DELETE /bucket/buckets/{bucket}/files/{filename}
```

**200 OK**
```json
{ "deleted": "photo.jpg", "bucket": "my-bucket" }
```

`404` if the file doesn't exist. There is no endpoint to delete a bucket; remove
the empty folder on the server if you need to.

### Names with special characters

URL-encode bucket and file names (`my file.txt` becomes `my%20file.txt`). Names are
decoded before validation.

## Errors

Errors return a JSON body: `{ "error": "<message>" }`.

| Status | Meaning |
|---|---|
| 400 | Invalid bucket/file name, not multipart, no file part, empty or incomplete body |
| 401 | Missing or wrong token |
| 404 | Unknown route, bucket or file |
| 411 | Upload without `Content-Length` |
| 413 | Upload larger than `MAX_UPLOAD` |
| 500 | Unexpected server error |

## Security

**Path traversal protection.** Bucket and file names are decoded and then must be
identical to `Path(name).name`. This rejects `..`, `.`, empty names, NUL bytes,
backslashes, and anything containing `/`, including encoded forms like `..%2f`
and `%2e%2e`. As a second layer, each resolved path must sit directly inside its
parent folder (`storage/` for buckets, the bucket folder for files). This also
stops symlink escapes. Rejected names return `400`.

**Uploaded filenames** are reduced to their last component before validation, so a
hostile `filename="../../etc/cron.d/x"` is saved as `x` inside the bucket.

**Recommendations**
- Set a long random token: `openssl rand -hex 32`. Never keep the default.
- Don't commit the token to git. Pass it via environment or a systemd `EnvironmentFile`.
- Use a real TLS certificate (for example from a reverse proxy) for internet exposure.
- Remember that anyone with the token has full read, write and delete access to all buckets.

## Limits and behavior notes

- Max upload: 2 GiB per request (`MAX_UPLOAD`). Bodies are spooled to disk, not RAM.
- One file per upload request; extra file parts are ignored.
- Dotfiles are allowed as names. In-progress uploads exist briefly as hidden
  `.<name>.<pid>.<thread>.part` files and may show up in a list during that time.
- Connections are closed after each request (HTTP/1.0 style) and time out after 120 s of inactivity.
- Request logging is disabled.

## Testing

`testapi.py` is a stdlib-only smoke test suite covering auth, the full upload,
list, download and delete cycle, binary integrity, and path traversal attempts.

```bash
IJUS_TOKEN=yourtoken python testapi.py --insecure          # https://pink.ijadium.com
python testapi.py --host localhost --http --port 8080 --token yourtoken
```

Flags: `--host`, `--port`, `--token`, `--insecure` (skip TLS verification),
`--http` (use plain HTTP). It exits with code 1 if any check fails. It leaves one
empty `storage/testapi-xxxxxxxx` folder per run that you can remove.

## Running as a service

`/etc/systemd/system/ijads.service`:

```ini
[Unit]
Description=ijads pink node + Ijus storage
After=network.target

[Service]
User=ijads
WorkingDirectory=/home/ijads/test/home
EnvironmentFile=/etc/ijads.env
ExecStart=/home/ijads/aimo/bin/python app1.py
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`/etc/ijads.env` (set permissions with `chmod 600`):

```
IJUS_TOKEN=your-long-random-token
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ijads
journalctl -u ijads -f
```

`AmbientCapabilities=CAP_NET_BIND_SERVICE` lets the service bind ports 80 and 443
without running as root. Adjust `ExecStart` to your venv's Python path.

## Troubleshooting

**`PermissionError: [Errno 13] Permission denied` on startup.**
Ports below 1024 need privileges. Use one of:
- `sudo IJUS_TOKEN=... "$(which python)" app1.py`
- `sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80`
- `sudo setcap 'cap_net_bind_service=+ep' "$(readlink -f "$(which python)")"`
- the systemd unit above

**`IJUS_TOKEN: command not found`.**
Bash doesn't allow spaces around `=`. Write `IJUS_TOKEN=value` or
`export IJUS_TOKEN=value`. With sudo, put the variable after `sudo`
(or use `sudo -E`), otherwise it is dropped.

**Every request returns 401.**
The server and client tokens differ. Check for stray quotes or whitespace, and
make sure the server was actually started with `IJUS_TOKEN` set (look for the
default-token warning at startup).

**TLS or certificate errors from clients.**
The auto-generated certificate is self-signed. Use `curl -k` or `--insecure`, or
place a trusted `cert.pem` and `key.pem` next to the script.

**`Address already in use`.**
Another web server (nginx, apache) is using the port. Stop it or change the ports in `serve(...)`.

**Large uploads fail with 413 or time out.**
Raise `MAX_UPLOAD`. If a reverse proxy sits in front, raise its body-size limit too
(for nginx: `client_max_body_size`).