# IJANT Public Gallery & Cloud Sync

`ijant_public` is the cloud distribution module for the IJANT Face Organiser. It synchronizes confirmed people and assigned photos from the local IJANT database into a public cloud stack:

* **Neon PostgreSQL**: Serves as the cloud database storing public people, image metadata, associations, and face bounding boxes.
* **Backblaze B2**: Stores original assigned image files securely in private object storage.
* **Flask Gallery (`displayerv2.py`)**: A modern web viewer with face-box HUD overlays, on/off toggling, and fast navigation, streaming images via short-lived presigned URLs.

---

## Architecture & Privacy Guarantee

```
[Local Machine]
  ijant.sqlite3 (Read-Only)  ───────────┐
                                        │
                                        ▼
                                  uploader.py
                                  /         \
               (Images only)     /           \  (Metadata & Bounding Boxes only)
                                ▼             ▼
                        Backblaze B2       Neon PostgreSQL
                         (Private)         (Public DB)
                                \             /
                                 ▼           ▼
                                displayerv2.py (Public Gallery Web App)
```

1. **Biometric Privacy**: Normalized face embeddings and internal ML data are **never** uploaded. Only confirmed people names, image metadata, and face bounding box coordinates `(x1, y1, x2, y2)` are published to Neon.
2. **Master DB Safety**: `ijant.sqlite3` is opened strictly read-only (`?mode=ro`). No database file is ever uploaded to B2.
3. **Private Storage**: Images in B2 are kept in a private bucket and served strictly via expiring S3v4 presigned URLs.

---

## B2 Storage Structure & How SQL Connects to It

### 1. B2 Storage Key Design (Zero Collisions & Deduplication)
When you organize photos with IJANT, the original pictures often come from different folders (e.g. `2024 Trip/DSCN5901.JPG` and `School Event/DSCN5901.JPG`). Storing them by their raw file name in object storage would cause severe collisions and file overwrites.

To prevent this, every object in Backblaze B2 is named using its **SHA-256 content digest**:

```
B2 Bucket (ijantdata)
└── ijant/
    └── images/
        ├── 960f0a175a4a953e70f408c958c116c118acf31d3193f22e05cdd04a05b14eb9.jpg
        ├── c31eb93bfc9684746a2dd1f40e60d2eb6566d3e77b353f2f9f2f325dd1f4faa4.jpg
        └── a28906763fa666c118ed26470b82a3af7f8ffdf144dd9b5ca1233002a590041f.jpg
```

* **Collision-Proof**: Two photos with the same file name from different folders produce different SHA-256 hashes, so both live safely side-by-side in B2.
* **Automatic Deduplication**: If the exact same photo is present in multiple folders, it has the identical hash and is only uploaded once to B2.

---

### 2. How the SQL Database (Neon PostgreSQL) Connects to B2

Neon stores the relational data, human-friendly names, folder paths, and face coordinates, referencing B2 through the `object_key` column:

```
[Neon Database: images table]
┌──────────┬─────────────────────────────────────────────────┬───────────────┬───────────────────────────┐
│ image_id │ object_key (Points to B2)                       │ original_name │ rel_path                  │
├──────────┼─────────────────────────────────────────────────┼───────────────┼───────────────────────────┤
│ 1        │ ijant/images/960f0a175a4a953e...b14eb9.jpg      │ DSCN5901.JPG  │ 10S2 Pictures\DSCN5901.JPG│
│ 2        │ ijant/images/c31eb93bfc968474...f4faa4.jpg      │ DSCN5902.JPG  │ 10S2 Pictures\DSCN5902.JPG│
└──────────┴─────────────────────────────────────────────────┴───────────────┴───────────────────────────┘
```

#### Complete Relational Schema in Neon:
```
people (person_id, name)
   │
   ├──< person_images (person_id, image_id) >──┐
   │                                           │
   └──< face_boxes (person_id, image_id,       │
                    x1, y1, x2, y2,            ▼
                    quality, det_conf) ──> images (image_id, object_key,
                                                   original_name, rel_path,
                                                   size_bytes, sha256,
                                                   width, height)
                                                       │
                                                       ▼
                                         Backblaze B2 Object Storage
                                        (Holds the actual binary file)
```

---

### 3. How the Public Web App (`displayerv2.py`) Bridges Neon & B2

When `displayerv2.py` is hosted online (e.g. on Vercel), it has **no access** to your local computer or `C:\` drive. Here is how it serves high-res images securely:

1. **User opens a person's gallery**:
   * The browser requests `/api/public/people/1`.
   * `displayerv2.py` queries Neon for person #1's photos and their face bounding box coordinates `(x1, y1, x2, y2)`.
2. **Browser requests an image (`<img src="/photo/42">`)**:
   * `displayerv2.py` queries Neon: `SELECT object_key FROM images WHERE image_id = 42`.
   * Using its B2 credentials, `displayerv2.py` creates an authenticated, short-lived **presigned URL** pointing directly to Backblaze B2 (valid for 1 hour).
   * It sends an HTTP `302 Redirect` to the browser.
3. **Browser loads the image from B2**:
   * The browser downloads the high-res image directly from Backblaze B2's high-speed CDN.
   * **Benefit**: The web server never downloads or buffers large image files, making it ultra-fast and lightweight.

---

### 4. The Upload & Synchronization Flow (`uploader.py`)

When you run `python uploader.py` on your local PC:

1. **Extracts Local Data**: Reads `ijant.sqlite3` read-only to find confirmed people, assigned images, and face boxes.
2. **Pre-flight Checks**:
   * Scans B2 to see which object keys already exist.
   * Scans Neon to see which image IDs are already indexed.
3. **De-duplication**: Any photo already stored in B2 and Neon is marked as `[SKIP]`. Zero bytes are wasted.
4. **Parallel Upload**: Any missing photos are uploaded to B2 using multi-threaded workers with a live Rich progress bar (showing photos/sec and remaining counts).
5. **Fresh Atomic Neon Sync**: Opens a fresh connection to Neon and commits:
   * `people` table (upserting names)
   * `images` table (linking `image_id` $\rightarrow$ B2 `object_key` + folder `rel_path`)
   * `person_images` (many-to-many relationships)
   * `face_boxes` (face bounding coordinates, quality, and detection confidence)
   * Local SQLite index replica (`public.ijant.sqlite`).

## Configuration (`.env`)

Store credentials in `ijant_public/.env`:

```env
# Backblaze B2 Configuration
B2_KEY_ID='your-b2-key-id'
B2_APPLICATION_KEY='your-b2-application-key'
B2_BUCKET_NAME='ijantdata'
B2_ENDPOINT_URL='s3.us-east-005.backblazeb2.com'
B2_REGION='us-west-004'

# Flask Gallery Server
GALLERY_HOST='127.0.0.1'
GALLERY_PORT=5050

# Neon PostgreSQL Database
NEON_DB_URL='postgresql://<user>:<password>@<neon-host>/neondb?sslmode=require'
```

### Optional Settings

| Variable | Default | Description |
|---|---|---|
| `PARALLEL_UPLOADS` | `20` | Concurrent worker threads used by `uploader.py` |
| `B2_PRESIGNED_URL_TTL` | `3600` | Expiration time (in seconds) for generated image links |
| `IJANT_DB_PATH` | `../ijant.sqlite3` | Custom path to local source SQLite database |
| `IJANT_PUBLIC_DB_PATH` | `./public.ijant.sqlite` | Custom path to local offline fallback index |

---

## Setup & Dependencies

Install required Python libraries (if not already present in your virtual environment):

```cmd
pip install boto3 b2sdk psycopg2 python-dotenv flask
```

---

## Database Setup & Management

### Initializing Database Tables with `sql_setter.py`
Before running the uploader for the first time or if you ever want to rebuild the database schema:

```cmd
cd c:\helper\IJANT\ijant_public
python sql_setter.py
```

* To drop and recreate all tables fresh, run:
  ```cmd
  python sql_setter.py --reset
  ```

---

## Storage Management with `b2_resetter.py`
To safely purge all objects and versions in the configured Backblaze B2 bucket:

```cmd
python b2_resetter.py
```
*(Prompts for confirmation by asking you to type the bucket name before deleting anything.)*

---

## Usage

### 1. Synchronizing Data with `uploader.py`

Run the uploader whenever you want to update Neon and upload new photos:

```cmd
cd c:\helper\IJANT\ijant_public
python uploader.py
```

#### What `uploader.py` does:
* **Checks existing cloud state**: Queries Neon DB for all existing images and hashes.
* **Smart de-duplication**: If an image is already uploaded to B2 and indexed in Neon, it is **skipped** instantly without re-uploading.
* **Parallel B2 upload**: Uploads only new or modified image files using multi-threaded workers.
* **Atomic Neon DB sync**:
  * Upserts `people` list.
  * Inserts/updates `images` table.
  * Rebuilds `person_images` relationships and `face_boxes` coordinates.
  * Prunes stale records for unassigned photos (does not delete objects from B2).
* **Local Fallback**: Also updates `public.ijant.sqlite` as a local offline replica.

---

### 2. Launching the Public Gallery with `displayerv2.py`

Start the gallery server:

```cmd
cd c:\helper\IJANT\ijant_public
python displayerv2.py
```

Then open:
```
http://127.0.0.1:5050
```

#### Gallery Features:
* **People Grid**: Clean overview of all recognized people with cover thumbnails and photo counts.
* **Person Album**: Grid view of all photos for the selected person.
* **Full-Screen Lightbox**:
  * High-resolution viewer streaming directly from Backblaze B2.
  * **Face Box HUD**: Displays bounding boxes highlighting assigned faces with dynamic image scaling.
  * **HUD Toggle Button**: Click **"Hide face boxes" / "Show face boxes"** to turn the HUD overlay on or off.
  * **Keyboard Controls**:
    * `←` (Left Arrow): Previous photo
    * `→` (Right Arrow): Next photo
    * `Esc`: Close lightbox
  * **Instant Refresh**: Use the **↻ Refresh Gallery** button to reload without restarting the server.

---

## Neon Database Schema Reference

The following PostgreSQL tables are created automatically on Neon upon first run:

```sql
-- People
CREATE TABLE people (
    person_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- Images
CREATE TABLE images (
    image_id INTEGER PRIMARY KEY,
    object_key TEXT NOT NULL,
    original_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- Person to Image link
CREATE TABLE person_images (
    person_id INTEGER NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
    image_id INTEGER NOT NULL REFERENCES images(image_id) ON DELETE CASCADE,
    PRIMARY KEY (person_id, image_id)
);

-- Face Bounding Boxes
CREATE TABLE face_boxes (
    id BIGSERIAL PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
    image_id INTEGER NOT NULL REFERENCES images(image_id) ON DELETE CASCADE,
    x1 REAL NOT NULL,
    y1 REAL NOT NULL,
    x2 REAL NOT NULL,
    y2 REAL NOT NULL
);
```
