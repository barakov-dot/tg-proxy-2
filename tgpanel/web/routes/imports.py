"""Import of existing proxy profiles (PLAN 3.10): preview, edit, confirm."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from starlette.datastructures import FormData

from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.importer import (
    OLD_BOT_WARNING,
    ImportPreview,
    ImportSourceError,
    RowEdit,
)
from tgpanel.domain.import_ import DEFAULT_ID_REGEX
from tgpanel.web.routes.common import (
    HttpError,
    auth_of,
    clean,
    fraw,
    fstr,
    get_web,
    load_form,
    redirect,
    render,
    require_auth,
    upload_text,
)
from tgpanel.web.texts import T

router = APIRouter(dependencies=[Depends(require_auth)])
MAX_ROWS = 5000


@router.get("/import")
async def import_form(request: Request) -> Response:
    return await render(
        request,
        "import.html",
        page="import",
        regex=DEFAULT_ID_REGEX,
        warning=OLD_BOT_WARNING,
        error=None,
    )


def _edit_lines(form: FormData) -> tuple[list[str], dict[str, RowEdit], list[str]]:
    """Per-row inputs -> (csv lines for 'recalculate', edits for 'confirm', errors)."""
    n_raw = fstr(form, "n")
    n = int(n_raw) if n_raw.isdigit() and int(n_raw) <= MAX_ROWS else 0
    lines: list[str] = []
    edits: dict[str, RowEdit] = {}
    errors: list[str] = []
    for i in range(n):
        src = fstr(form, f"src_{i}")
        if not src:
            continue
        tg_raw = fstr(form, f"tg_{i}")
        orig_tg = fstr(form, f"otg_{i}")
        dn = fstr(form, f"dn_{i}")
        odn = fstr(form, f"odn_{i}")
        cm = fstr(form, f"cm_{i}")
        ocm = fstr(form, f"ocm_{i}")
        skip = fstr(form, f"skip_{i}") == "1"
        if tg_raw and (not tg_raw.isdigit() or len(tg_raw) > 16):
            errors.append(T["import_bad_tg"].format(name=src))
            continue
        tg_id = int(tg_raw) if tg_raw and tg_raw != orig_tg else None
        changed_dn = dn if dn and dn != odn else None
        changed_cm = cm if cm != ocm else None
        if skip or tg_id is not None or changed_dn is not None or changed_cm is not None:
            edits[src] = RowEdit(
                tg_id=tg_id, display_name=changed_dn, comment=changed_cm, skip=skip
            )
        if not skip and (tg_id is not None or changed_dn is not None or changed_cm is not None):
            if ";" in src or ";" in dn:
                errors.append(T["import_semicolon"].format(name=src))
                continue
            lines.append(f"{src};{tg_raw if tg_id is not None else ''};{changed_dn or ''};{cm}")
    return lines, edits, errors


async def _preview(
    request: Request, csv_text: str, regex: str, notes: list[str] | None = None
) -> tuple[ImportPreview | None, str | None]:
    web = get_web(request)
    try:
        return await web.app.importer.preview(csv_text=csv_text or None, id_regex=regex), None
    except ImportSourceError as exc:
        return None, clean(str(exc))
    except (OperationRejected, ValueError) as exc:
        return None, clean(str(exc))


@router.post("/import/preview")
async def import_preview(request: Request) -> Response:
    form = await load_form(request)
    regex = fstr(form, "regex") or DEFAULT_ID_REGEX
    csv_text = fraw(form, "csv_text")
    uploaded = await upload_text(form, "csv_file")
    if uploaded:
        csv_text = (csv_text + "\n" + uploaded) if csv_text else uploaded
    errors: list[str] = []
    if fstr(form, "action") == "recalc":
        lines, _, errors = _edit_lines(form)
        if lines:
            csv_text = (csv_text + "\n" if csv_text else "") + "\n".join(lines)
    preview, error = await _preview(request, csv_text, regex)
    if preview is None:
        return await render(
            request,
            "import.html",
            422,
            page="import",
            regex=regex,
            warning=OLD_BOT_WARNING,
            error=error,
        )
    return await render(
        request,
        "import_preview.html",
        page="import",
        p=preview,
        regex=regex,
        csv_text=csv_text,
        notes=errors,
    )


@router.post("/import/confirm")
async def import_confirm(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    if fstr(form, "ack_old_bot") != "1":
        return redirect(request, "/import", ("err", T["import_ack_needed"]))
    regex = fstr(form, "regex") or DEFAULT_ID_REGEX
    csv_text = fraw(form, "csv_text")
    _, edits, errors = _edit_lines(form)
    if errors:
        return redirect(request, "/import", ("err", "; ".join(errors)))
    preview, error = await _preview(request, csv_text, regex)
    if preview is None:
        return redirect(request, "/import", ("err", error or T["operation_failed"]))
    try:
        result = await web.app.importer.confirm(preview, edits, actor=auth.actor)
    except (OperationRejected, ImportSourceError) as exc:
        return redirect(request, "/import", ("err", clean(str(exc))))
    if not result.ok:
        return redirect(request, "/import", ("err", clean(result.error) or T["operation_failed"]))
    return redirect(
        request,
        "/users",
        ("ok", T["import_done"].format(imported=result.imported, skipped=result.skipped)),
    )


__all__ = ["HttpError", "router"]
