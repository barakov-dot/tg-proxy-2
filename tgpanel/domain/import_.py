"""Pure planning of the import of existing proxy profiles (PLAN 3.10). No I/O."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from tgpanel.domain.models import CarrierMode, clean_display_name
from tgpanel.domain.secrets_ import ParsedSecret, parse_imported_secret

SENTINEL_NAME = "_tgpanel_sentinel"
DEFAULT_ID_REGEX = r"^user_(\d{5,15})$"
IMPORT_COMMENT = "import"
SKIP_KNOWN = "secret already known"
MAX_REGEX_LENGTH = 200


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """A profile from the proxy's profiles.json (its `limits` block is dropped by the caller)."""

    name: str
    secret: str = field(repr=False)
    carrier_mode: str | None
    backend: str


@dataclass(frozen=True, slots=True)
class CsvRow:
    profile_name: str
    tg_id: int | None = None
    display_name: str = ""
    comment: str = ""


@dataclass(frozen=True, slots=True)
class PlanRow:
    source_name: str
    tg_id: int | None
    display_name: str  # label for people (may be empty); the user `name` is source_name
    comment: str
    secret: str = field(repr=False)  # kept as-is (with 'dd' prefix if it had one)
    base_secret: str = field(repr=False)
    carrier_mode: CarrierMode | None
    source_backend: str
    skip_reason: str | None = None

    @property
    def name(self) -> str:
        """Technical user name: always the source profile name (never replaced by the CSV)."""
        return self.source_name

    @property
    def will_import(self) -> bool:
        return self.skip_reason is None


@dataclass(frozen=True, slots=True)
class ImportPlan:
    rows: tuple[PlanRow, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    unused_mtproxy_secrets: tuple[str, ...] = ()  # masked, never full secrets
    sentinel_ignored: bool = False

    @property
    def importable(self) -> tuple[PlanRow, ...]:
        return tuple(r for r in self.rows if r.will_import)

    @property
    def blocked(self) -> bool:
        return bool(self.errors)


MAX_MATCH_INPUT = 64  # relay profile names are at most 64 characters
_LIT = r"(?:[^\\.^$*+?{}\[\]|()\n\r]|\\[.\-_/:@ ^$*+?{}\[\]|()\\])"
# ^<literal prefix>(\d{n,m} | \d{n} | \d+)<literal suffix>$ and nothing else: such a pattern
# is linear (one literal run, one digit run, one literal run), so it cannot backtrack badly.
_ID_PATTERN_RE = re.compile(
    rf"\^({_LIT}*)\(\\d(?:\+|\{{(\d{{1,3}})(?:,(\d{{1,3}}))?\}})\)({_LIT}*)\$"
)
ID_REGEX_ERROR = (
    "Выражение для Telegram ID допускает только вид ^префикс(\\d{n,m})суффикс$ "
    "или ^префикс(\\d+)суффикс$; префикс и суффикс - обычные символы"
)


def check_id_regex_safety(pattern: str) -> str | None:
    """Reason (Russian) if the pattern is outside the restricted grammar, else None.

    Accepted: ``^<literal>(\\d{n,m}|\\d{n}|\\d+)<literal>$`` (escape-aware literals). Anything
    else - alternation, groups, classes, other quantifiers, look-arounds - is refused, so no
    accepted pattern can backtrack catastrophically.
    """
    match = _ID_PATTERN_RE.fullmatch(pattern)
    if match is None:
        return ID_REGEX_ERROR
    low, high = match.group(2), match.group(3)
    if low is not None and (int(low) < 1 or (high is not None and int(high) < int(low))):
        return "В выражении для Telegram ID неверные границы количества цифр"
    return None


def mask_secret(secret: str) -> str:
    return secret[:4] + "..." if len(secret) > 4 else "..."


def parse_mtproxy_secrets_text(text: str) -> set[str]:
    """Secrets from mtproxy.secrets: one per line, blanks and '#' comments ignored."""
    out: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(_norm(line))
    return out


def parse_csv_rows(text: str) -> tuple[list[CsvRow], list[str]]:
    """Parse `profile;telegram_id;display name;comment` lines. Returns (rows, errors).

    The 3rd column is the user's DISPLAY name; the technical name stays the profile name.
    """
    rows: list[CsvRow] = []
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in line.split(";")]
        parts += [""] * (4 - len(parts))
        name, raw_id, display, comment = parts[0], parts[1], parts[2], ";".join(parts[3:])
        if not name:
            errors.append(f"csv line {lineno}: empty profile name")
            continue
        tg_id: int | None = None
        if raw_id:
            if not raw_id.isdigit():
                errors.append(f"csv line {lineno}: invalid telegram id")
                continue
            tg_id = int(raw_id)
        rows.append(CsvRow(name, tg_id, display, comment))
    return rows, errors


def _norm(secret: str) -> str:
    s = secret.strip().lower()
    return s[2:] if len(s) == 34 and s.startswith("dd") else s


def plan_import(
    profiles: Iterable[SourceProfile],
    *,
    mtproxy_secrets: Collection[str] | None = None,
    existing_secrets: Collection[str] = (),
    existing_tg_ids: Collection[int] = (),
    id_regex: str = DEFAULT_ID_REGEX,
    csv_rows: Iterable[CsvRow] = (),
) -> ImportPlan:
    """Build the import plan. ``mtproxy_secrets`` = union of mtproxy.secrets and
    MTPROXY_SECRET, or None when unavailable (no reconciliation)."""
    pattern: re.Pattern[str] | None = None
    regex_error: str | None = None
    if len(id_regex) > MAX_REGEX_LENGTH:
        regex_error = f"Выражение для Telegram ID длиннее {MAX_REGEX_LENGTH} символов"
    else:
        unsafe = check_id_regex_safety(id_regex)
        if unsafe is not None:
            regex_error = unsafe
        try:
            pattern = None if unsafe else re.compile(id_regex, re.ASCII)
        except re.error as exc:
            regex_error = f"Выражение для Telegram ID некорректно: {exc}"
        else:
            if pattern is not None and pattern.groups < 1:
                regex_error = "Выражение для Telegram ID должно содержать группу с цифрами"
                pattern = None
    existing_tg_set = set(existing_tg_ids)
    mtproxy_norm = None if mtproxy_secrets is None else {_norm(s) for s in mtproxy_secrets}
    csv_by_name = {r.profile_name: r for r in csv_rows}
    known = {_norm(s) for s in existing_secrets}
    warnings: list[str] = []
    errors: list[str] = []
    rows: list[PlanRow] = []
    sentinel_ignored = False
    seen_profile_bases: set[str] = set()
    parsed: list[tuple[SourceProfile, ParsedSecret]] = []
    if regex_error is not None:
        errors.append(regex_error)

    for prof in profiles:
        if prof.name == SENTINEL_NAME:
            sentinel_ignored = True
            continue
        try:
            sec = parse_imported_secret(prof.secret)
        except ValueError:
            errors.append(f"{prof.name}: unsupported secret format")
            continue
        parsed.append((prof, sec))

    dup_bases = {b for b, n in Counter(s.base for _, s in parsed).items() if n > 1}
    for prof, sec in parsed:
        if sec.base in dup_bases:
            errors.append(f"{prof.name}: duplicate secret among source profiles")

    for prof, sec in parsed:
        seen_profile_bases.add(sec.base)
        csv = csv_by_name.get(prof.name)
        tg_id: int | None = None
        if csv is not None and csv.tg_id is not None:
            tg_id = csv.tg_id
        else:
            m = pattern.search(prof.name[:MAX_MATCH_INPUT]) if pattern is not None else None
            g = m.group(1) if m else None
            if g is not None and g.isascii() and g.isdigit():
                tg_id = int(g)
            elif pattern is not None:
                warnings.append(f"{prof.name}: telegram id not recognized")
        mode: CarrierMode | None = None
        if prof.carrier_mode:
            try:
                mode = CarrierMode(prof.carrier_mode)
            except ValueError:
                errors.append(f"{prof.name}: unknown carrier_mode")
        display = ""
        if csv is not None:
            try:
                display = clean_display_name(csv.display_name)
            except ValueError as exc:
                errors.append(f"{prof.name}: {exc}")
        comment = IMPORT_COMMENT
        if csv and csv.comment:
            comment = f"{IMPORT_COMMENT}; {csv.comment}"
        skip = SKIP_KNOWN if sec.base in known else None
        if skip is None and mtproxy_norm is not None and sec.base not in mtproxy_norm:
            warnings.append(f"{prof.name}: secret not found in MTProxy, profile probably broken")
        rows.append(
            PlanRow(
                source_name=prof.name,
                tg_id=tg_id,
                display_name=display,
                comment=comment,
                secret=sec.raw,
                base_secret=sec.base,
                carrier_mode=mode,
                source_backend=prof.backend,
                skip_reason=skip,
            )
        )

    profile_names = {p.name for p, _ in parsed}
    for name in csv_by_name:
        if name not in profile_names:
            warnings.append(f"csv: profile {name} not found among source profiles")

    active_rows = [r for r in rows if r.will_import]
    id_counts = Counter(r.tg_id for r in active_rows if r.tg_id is not None)
    for tg, n in id_counts.items():
        if n > 1:
            names = ", ".join(r.source_name for r in active_rows if r.tg_id == tg)
            errors.append(f"duplicate telegram id {tg}: {names}")
    for r in active_rows:
        if r.tg_id is not None and r.tg_id in existing_tg_set:
            errors.append(f"{r.source_name}: telegram id {r.tg_id} already belongs to a user")

    unused: tuple[str, ...] = ()
    if mtproxy_norm is not None:
        unused = tuple(mask_secret(s) for s in sorted(mtproxy_norm - seen_profile_bases - known))
        for masked in unused:
            warnings.append(f"MTProxy secret {masked} is unused by any profile, not imported")

    return ImportPlan(
        rows=tuple(rows),
        warnings=tuple(warnings),
        errors=tuple(errors),
        unused_mtproxy_secrets=unused,
        sentinel_ignored=sentinel_ignored,
    )
