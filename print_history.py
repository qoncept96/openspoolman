import os
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_DB_NAME = "3d_printer_logs.db"
DB_ENV_VAR = "OPENSPOOLMAN_PRINT_HISTORY_DB"


def _default_db_path() -> Path:
    """Resolve the print history database path, allowing an env override."""

    env_path = os.getenv(DB_ENV_VAR)
    if env_path:
        return Path(env_path).expanduser().resolve()

    return Path(__file__).resolve().parent / "data" / DEFAULT_DB_NAME


db_config = {"db_path": str(_default_db_path())}  # Configuration for database location


def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cursor.fetchall()}
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def create_database() -> None:
    """
    Ensure the SQLite schema exists (used for both fresh and upgrading databases).
    """
    db_path = Path(db_config["db_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS prints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            print_date TEXT NOT NULL,
            file_name TEXT NOT NULL,
            print_type TEXT NOT NULL,
            image_file TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS filament_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            print_id INTEGER NOT NULL,
            spool_id INTEGER,
            filament_type TEXT NOT NULL,
            color TEXT NOT NULL,
            grams_used REAL NOT NULL,
            ams_slot INTEGER NOT NULL,
            estimated_grams REAL,
            length_used REAL,
            estimated_length REAL,
            FOREIGN KEY (print_id) REFERENCES prints (id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS print_layer_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            print_id INTEGER NOT NULL UNIQUE,
            total_layers INTEGER,
            layers_printed INTEGER,
            filament_grams_billed REAL,
            filament_grams_total REAL,
            status TEXT NOT NULL DEFAULT 'RUNNING',
            predicted_end_time TEXT,
            actual_end_time TEXT,
            FOREIGN KEY (print_id) REFERENCES prints (id) ON DELETE CASCADE
        )
    ''')

    _ensure_column(
        cursor,
        "filament_usage",
        "estimated_grams",
        "REAL",
    )
    _ensure_column(
        cursor,
        "filament_usage",
        "length_used",
        "REAL",
    )
    _ensure_column(
        cursor,
        "filament_usage",
        "estimated_length",
        "REAL",
    )
    # JSON list of any *other* spools billed for this slot, written when a spool is
    # swapped out mid-print. The row's own spool_id keeps the first one.
    _ensure_column(
        cursor,
        "filament_usage",
        "prior_spool_ids",
        "TEXT",
    )

    # Ensure column definitions exist for older databases
    _ensure_column(
        cursor,
        "print_layer_tracking",
        "predicted_end_time",
        "TEXT",
    )
    _ensure_column(
        cursor,
        "print_layer_tracking",
        "actual_end_time",
        "TEXT",
    )

    conn.commit()
    conn.close()


def insert_print(file_name: str, print_type: str, image_file: str = None, print_date: str = None) -> int:
    """
    Inserts a new print job into the database and returns the print ID.
    If no print_date is provided, the current timestamp is used.
    """
    if print_date is None:
        print_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO prints (print_date, file_name, print_type, image_file)
        VALUES (?, ?, ?, ?)
    ''', (print_date, file_name, print_type, image_file))
    print_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return print_id

def insert_filament_usage(
    print_id: int,
    filament_type: str,
    color: str,
    grams_used: float,
    ams_slot: int,
    estimated_grams: float | None = None,
    length_used: float | None = None,
    estimated_length: float | None = None,
) -> None:
    """
    Inserts a new filament usage entry for a specific print job.
    """
    conn = sqlite3.connect(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO filament_usage (print_id, filament_type, color, grams_used, ams_slot, estimated_grams, length_used, estimated_length)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (print_id, filament_type, color, grams_used, ams_slot, estimated_grams, length_used, estimated_length))
    conn.commit()
    conn.close()

def update_filament_spool(print_id: int, filament_id: int, spool_id: int) -> None:
    """
    Updates the spool_id for a given filament usage entry, ensuring it belongs to the specified print job.
    """
    conn = sqlite3.connect(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE filament_usage
        SET spool_id = ?
        WHERE ams_slot = ? AND print_id = ?
    ''', (spool_id, filament_id, print_id))
    conn.commit()
    conn.close()

def update_filament_grams_used(print_id: int, filament_id: int, grams_used: float, length_used: float | None = None) -> None:
    """
    Updates the grams_used (and optional length_used) for a given filament usage entry, ensuring it belongs to the specified print job.
    """
    set_parts = ["grams_used = ?"]
    params: list[float | int] = [grams_used]
    if length_used is not None:
        set_parts.append("length_used = ?")
        params.append(length_used)

    set_clause = ", ".join(set_parts)
    params.extend([filament_id, print_id])

    conn = sqlite3.connect(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute(f'''
        UPDATE filament_usage
        SET {set_clause}
        WHERE ams_slot = ? AND print_id = ?
    ''', params)
    conn.commit()
    conn.close()


def get_prints_with_filament(limit: int | None = None, offset: int | None = None,
                             filter_key: str | None = None, live_print_id: int | None = None):
    """
    Retrieves print jobs along with their associated filament usage, grouped by print job.

    A total count is returned to support pagination. `filter_key` is one of
    ATTENTION_FILTERS and narrows both the page and the count, so the pager stays
    correct when a filter is applied.
    """
    where_sql = ATTENTION_FILTERS.get(resolve_filter(filter_key))
    where_clause = f" WHERE {where_sql}" if where_sql else ""
    filter_params = {"live_print_id": live_print_id}

    conn = sqlite3.connect(db_config["db_path"])
    conn.row_factory = sqlite3.Row  # Enable column name access

    count_cursor = conn.cursor()
    count_cursor.execute(f"SELECT COUNT(*) FROM prints p{where_clause}", filter_params)
    total_count = count_cursor.fetchone()[0]

    cursor = conn.cursor()
    query = '''
        SELECT p.id AS id, p.print_date AS print_date, p.file_name AS file_name,
               p.print_type AS print_type, p.image_file AS image_file,
       (
           SELECT json_group_array(json_object(
               'spool_id', f.spool_id,
                'filament_type', f.filament_type,
                'color', f.color,
                'grams_used', f.grams_used,
                'estimated_grams', f.estimated_grams,
                'length_used', f.length_used,
                'estimated_length', f.estimated_length,
                'ams_slot', f.ams_slot
            )) FROM filament_usage f WHERE f.print_id = p.id
        ) AS filament_info
        FROM prints p
    ''' + where_clause + '''
        ORDER BY p.print_date DESC
    '''
    # Named params throughout so the filter's :live_print_id can coexist with
    # the pagination values.
    params: dict = dict(filter_params)
    if limit is not None:
        query += " LIMIT :limit"
        params["limit"] = limit
        if offset is not None:
            query += " OFFSET :offset"
            params["offset"] = offset

    cursor.execute(query, params)
    prints = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return prints, total_count

def get_prints_by_spool(spool_id: int):
    """
    Retrieves all print jobs that used a specific spool.
    """
    conn = sqlite3.connect(db_config["db_path"])
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT DISTINCT p.* FROM prints p
        JOIN filament_usage f ON p.id = f.print_id
        WHERE f.spool_id = ?
    ''', (spool_id,))
    prints = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return prints

def get_filament_for_slot(print_id: int, ams_slot: int):
  conn = sqlite3.connect(db_config["db_path"])
  conn.row_factory = sqlite3.Row  # Enable column name access
  cursor = conn.cursor()

  cursor.execute('''
      SELECT * FROM filament_usage
      WHERE print_id = ? AND ams_slot = ?
  ''', (print_id, ams_slot))

  row = cursor.fetchone()
  conn.close()
  return dict(row) if row else None

def _ensure_layer_tracking_entry(print_id: int):
  conn = sqlite3.connect(db_config["db_path"])
  cursor = conn.cursor()
  cursor.execute('''
      INSERT OR IGNORE INTO print_layer_tracking (print_id)
      VALUES (?)
  ''', (print_id,))
  conn.commit()
  conn.close()

def update_layer_tracking(print_id: int, **fields):
  if not fields:
    return

  allowed_columns = {
      "total_layers",
      "layers_printed",
      "filament_grams_billed",
      "filament_grams_total",
      "status",
      "predicted_end_time",
      "actual_end_time",
  }

  sanitized = {key: value for key, value in fields.items() if key in allowed_columns}
  if not sanitized:
    return

  _ensure_layer_tracking_entry(print_id)

  set_clause = ", ".join(f"{key} = ?" for key in sanitized)
  params = list(sanitized.values()) + [print_id]

  conn = sqlite3.connect(db_config["db_path"])
  cursor = conn.cursor()
  cursor.execute(f'''
      UPDATE print_layer_tracking
      SET {set_clause}
      WHERE print_id = ?
  ''', params)
  conn.commit()
  conn.close()

def get_layer_tracking_for_prints(print_ids: list[int]):
  if not print_ids:
    return {}

  conn = sqlite3.connect(db_config["db_path"])
  conn.row_factory = sqlite3.Row
  cursor = conn.cursor()
  placeholders = ",".join("?" for _ in print_ids)
  cursor.execute(f'''
      SELECT print_id, total_layers, layers_printed, filament_grams_billed, filament_grams_total, status, predicted_end_time, actual_end_time
      FROM print_layer_tracking
      WHERE print_id IN ({placeholders})
  ''', print_ids)
  rows = cursor.fetchall()
  conn.close()
  return {row["print_id"]: dict(row) for row in rows}

def get_all_filament_usage_for_print(print_id: int):
  """
  Retrieves all filament usage entries for a specific print.
  Returns a dict mapping ams_slot to a dict with grams_used and length_used.
  """
  conn = sqlite3.connect(db_config["db_path"])
  conn.row_factory = sqlite3.Row
  cursor = conn.cursor()

  cursor.execute('''
      SELECT ams_slot, grams_used, length_used, spool_id FROM filament_usage
      WHERE print_id = ?
  ''', (print_id,))

  results = {
      row["ams_slot"]: {
          "grams_used": row["grams_used"],
          "length_used": row["length_used"],
          "spool_id": row["spool_id"],
      }
      for row in cursor.fetchall()
  }
  conn.close()
  return results

# ---------------------------------------------------------------------------
# "Needs attention" buckets
#
# A print needs attention when filament came off a spool and the record does not
# say so. Every predicate below is an EXISTS on `p.id` so the same SQL drives the
# JSON API, the counts, and the /print_history filter dropdown without being
# written twice. Named params so the nullable live-print id can be bound once.
#
# Status strings are literals on purpose: filament_usage_tracker imports this
# module, so importing its constants back would be a cycle.
# See filament_usage_tracker.LAYER_TRACKING_STATUS_*.
# ---------------------------------------------------------------------------

#: Statuses that mean the print is over, whatever the outcome.
FINISHED_STATUSES = ("COMPLETED", "ABORTED", "FAILED", "INTERRUPTED")

#: A slot that was never charged to any spool.
#:
#: COMPLETED and INTERRUPTED count on the slicer estimate alone. INTERRUPTED
#: means our own bookkeeping missed the finish, not that the print stopped
#: early, so the filament almost certainly did go through - and leaving it out
#: would hide exactly the prints this tool exists to catch. ABORTED and FAILED
#: need *measured* length: those really did stop part way, and their estimate
#: describes filament that was never extruded.
#:
#: Nothing is charged from this bucket. It only decides what gets surfaced; the
#: deduction happens when a spool is picked by hand.
_PRED_ORPHAN = """
EXISTS (
  SELECT 1 FROM filament_usage f
  JOIN print_layer_tracking t ON t.print_id = f.print_id
  WHERE f.print_id = p.id
    AND f.spool_id IS NULL
    AND (
          (t.status IN ('COMPLETED', 'INTERRUPTED')
           AND (COALESCE(f.length_used, 0) > 0 OR COALESCE(f.estimated_grams, 0) > 0))
       OR (t.status IN ('ABORTED', 'FAILED')
           AND COALESCE(f.length_used, 0) > 0)
        )
)
"""

#: Left on "Printing" forever, because the container was down when the print ended.
#: The live print is excluded by id rather than by any wall-clock guess - print
#: durations here run from minutes to days.
_PRED_STUCK = """
EXISTS (
  SELECT 1 FROM print_layer_tracking t
  WHERE t.print_id = p.id AND t.status = 'RUNNING'
)
AND (:live_print_id IS NULL OR p.id != :live_print_id)
"""

#: Spool bound, print finished, yet nothing was ever deducted.
_PRED_UNBILLED = """
EXISTS (
  SELECT 1 FROM filament_usage f
  JOIN print_layer_tracking t ON t.print_id = f.print_id
  WHERE f.print_id = p.id
    AND t.status = 'COMPLETED'
    AND f.spool_id IS NOT NULL
    AND COALESCE(f.grams_used, 0) <= 0
    AND COALESCE(f.estimated_grams, 0) > 0
)
"""

#: No layer-tracking row at all - the 3MF never parsed, so the other buckets
#: (which all join that table) cannot see it. Aged out so a print that started
#: moments ago is not reported before its tracking row exists, and narrowed to
#: prints that actually have something uncharged: the oldest rows here predate
#: layer tracking entirely and are already fully accounted for.
_PRED_UNTRACKED = """
NOT EXISTS (SELECT 1 FROM print_layer_tracking t WHERE t.print_id = p.id)
AND p.print_date < datetime('now', 'localtime', '-6 hours')
AND (
      NOT EXISTS (SELECT 1 FROM filament_usage f WHERE f.print_id = p.id)
   OR EXISTS (
        SELECT 1 FROM filament_usage f
        WHERE f.print_id = p.id
          AND (f.spool_id IS NULL OR COALESCE(f.grams_used, 0) <= 0)
      )
    )
"""

#: More than one spool was billed for a single slot, because the spool was swapped
#: mid-print. The row can only name one of them, so the others are recorded in
#: prior_spool_ids and the print is flagged for a human.
_PRED_SPLIT = """
EXISTS (
  SELECT 1 FROM filament_usage f
  WHERE f.print_id = p.id AND f.prior_spool_ids IS NOT NULL
)
"""

_BUCKET_PREDICATES = {
    "orphans": _PRED_ORPHAN,
    "stuck": _PRED_STUCK,
    "unbilled": _PRED_UNBILLED,
    "untracked": _PRED_UNTRACKED,
    "split": _PRED_SPLIT,
}

def _pred_attention() -> str:
  return " OR ".join(f"({sql.strip()})" for sql in _BUCKET_PREDICATES.values())

#: Keys accepted by /print_history?filter= and by the API. "all" is the default
#: and means no WHERE clause at all.
ATTENTION_FILTERS = {
    "all": None,
    "attention": None,  # filled in below; needs _pred_attention()
    "orphans": _PRED_ORPHAN,
    "stuck": _PRED_STUCK,
    "unbilled": _PRED_UNBILLED,
    "untracked": _PRED_UNTRACKED,
    "split": _PRED_SPLIT,
}
ATTENTION_FILTERS["attention"] = _pred_attention()

#: Labels for the dropdown, in display order.
ATTENTION_FILTER_LABELS = (
    ("all", "All prints"),
    ("attention", "Needs attention"),
    ("orphans", "No spool assigned"),
    ("stuck", "Stuck on printing"),
    ("unbilled", "Nothing deducted"),
    ("untracked", "Not tracked"),
    ("split", "Spool swapped mid-print"),
)

def resolve_filter(filter_key: str | None) -> str:
  """Normalise an untrusted ?filter= value to a known key, defaulting to 'all'."""
  if filter_key in ATTENTION_FILTERS:
    return filter_key
  return "all"


def get_attention_counts(live_print_id: int | None = None) -> dict:
  """Counts of prints per bucket, plus the distinct total. Counts prints, not rows:
  a two-colour print with two orphaned slots is one thing to go and fix."""
  conn = sqlite3.connect(db_config["db_path"])
  cursor = conn.cursor()
  params = {"live_print_id": live_print_id}

  counts = {}
  for name, predicate in _BUCKET_PREDICATES.items():
    cursor.execute(f"SELECT COUNT(*) FROM prints p WHERE {predicate}", params)
    counts[name] = cursor.fetchone()[0]

  cursor.execute(f"SELECT COUNT(*) FROM prints p WHERE {_pred_attention()}", params)
  counts["total"] = cursor.fetchone()[0]

  conn.close()
  return counts


def get_attention_items(live_print_id: int | None = None, limit: int = 20) -> tuple[list[dict], bool]:
  """The prints behind the counts, newest first, with enough detail to act on.

  Returns (items, truncated).
  """
  conn = sqlite3.connect(db_config["db_path"])
  conn.row_factory = sqlite3.Row
  cursor = conn.cursor()
  params = {"live_print_id": live_print_id, "limit": limit + 1}

  cursor.execute(f"""
      SELECT p.id AS print_id, p.print_date, p.file_name, p.image_file,
             t.status, t.layers_printed, t.total_layers, t.predicted_end_time,
             {_PRED_ORPHAN.strip()} AS is_orphan,
             {_PRED_STUCK.strip()} AS is_stuck,
             {_PRED_UNBILLED.strip()} AS is_unbilled,
             {_PRED_UNTRACKED.strip()} AS is_untracked,
             {_PRED_SPLIT.strip()} AS is_split
      FROM prints p
      LEFT JOIN print_layer_tracking t ON t.print_id = p.id
      WHERE {_pred_attention()}
      ORDER BY p.print_date DESC
      LIMIT :limit
  """, params)
  rows = [dict(row) for row in cursor.fetchall()]

  truncated = len(rows) > limit
  rows = rows[:limit]

  items = []
  for row in rows:
    kinds = [k for k in ("orphan", "stuck", "unbilled", "untracked", "split")
             if row.pop(f"is_{k}")]
    cursor.execute("""
        SELECT ams_slot, spool_id, prior_spool_ids, filament_type, color,
               grams_used, estimated_grams, length_used, estimated_length
        FROM filament_usage WHERE print_id = ? ORDER BY ams_slot
    """, (row["print_id"],))
    row["kinds"] = kinds
    row["slots"] = [dict(slot) for slot in cursor.fetchall()]
    items.append(row)

  conn.close()
  return items, truncated


def get_latest_print_date() -> str | None:
  """Newest print_date, as stored: naive, container-local. Callers that need an
  age should use get_latest_print_age_seconds() rather than parsing this as UTC."""
  conn = sqlite3.connect(db_config["db_path"])
  cursor = conn.cursor()
  cursor.execute("SELECT MAX(print_date) FROM prints")
  value = cursor.fetchone()[0]
  conn.close()
  return value


def get_latest_print_age_seconds() -> float | None:
  """Seconds since the newest print started, computed server-side so the caller
  never has to know this database's timezone."""
  latest = get_latest_print_date()
  if not latest:
    return None
  try:
    parsed = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S")
  except ValueError:
    return None
  return max(0.0, (datetime.now() - parsed).total_seconds())


def get_stale_running_print_ids(exclude_print_ids: list[int] | None = None) -> list[int]:
  """Prints still marked RUNNING, optionally excluding ones known to be live."""
  conn = sqlite3.connect(db_config["db_path"])
  cursor = conn.cursor()
  query = "SELECT print_id FROM print_layer_tracking WHERE status = 'RUNNING'"
  params: list[int] = []
  if exclude_print_ids:
    placeholders = ",".join("?" for _ in exclude_print_ids)
    query += f" AND print_id NOT IN ({placeholders})"
    params.extend(exclude_print_ids)
  cursor.execute(query, params)
  ids = [row[0] for row in cursor.fetchall()]
  conn.close()
  return ids


def close_stale_running_prints(status: str,
                               exclude_print_ids: list[int] | None = None,
                               actual_end_time: str | None = None) -> list[int]:
  """Close RUNNING rows the printer is demonstrably not running any more.

  Returns the print ids that were closed. COALESCE keeps an actual_end_time that
  somehow already exists.
  """
  stale = get_stale_running_print_ids(exclude_print_ids)
  if not stale:
    return []

  placeholders = ",".join("?" for _ in stale)
  conn = sqlite3.connect(db_config["db_path"])
  cursor = conn.cursor()
  cursor.execute(f"""
      UPDATE print_layer_tracking
      SET status = ?, actual_end_time = COALESCE(actual_end_time, ?)
      WHERE print_id IN ({placeholders})
  """, [status, actual_end_time] + stale)
  conn.commit()
  conn.close()
  return stale


def record_split_spool(print_id: int, ams_slot: int, spool_id: int) -> None:
  """Note that `spool_id` was also billed for this slot, on top of the spool the
  row names. Happens when a spool is swapped out mid-print; the schema has room
  for only one spool per slot, so the rest are recorded here and surfaced as the
  'split' bucket rather than silently overwriting the original attribution."""
  import json

  conn = sqlite3.connect(db_config["db_path"])
  cursor = conn.cursor()
  cursor.execute("""
      SELECT prior_spool_ids FROM filament_usage WHERE print_id = ? AND ams_slot = ?
  """, (print_id, ams_slot))
  row = cursor.fetchone()
  if row is None:
    conn.close()
    return

  try:
    existing = json.loads(row[0]) if row[0] else []
  except (TypeError, ValueError):
    existing = []
  if not isinstance(existing, list):
    existing = []

  if spool_id not in existing:
    existing.append(spool_id)
    cursor.execute("""
        UPDATE filament_usage SET prior_spool_ids = ?
        WHERE print_id = ? AND ams_slot = ?
    """, (json.dumps(existing), print_id, ams_slot))
    conn.commit()
  conn.close()


# Example for creating the database if it does not exist
create_database()
