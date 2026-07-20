"""
Runs under LibreOffice's bundled Python (the only one with a working `uno` module).
Connects to a headless LibreOffice instance via UNO, enables track changes (RecordChanges),
inserts text next to an anchor string, and saves the document in place.

Invoked as:
    <soffice_python> insert_tracked_text.py <params_json_path> <result_json_path>

params_json_path must contain a JSON object with:
    path: str          - absolute path to the .odt file to modify
    anchor_text: str    - existing text to search for as the insertion anchor
    new_text: str        - text to insert as a tracked change
    author: str | None  - "Given Family" name to attribute the change to (optional)
    insert_mode: str    - "after" (default), "before"

Writes a JSON object to result_json_path: {"success": true} or {"success": false, "error": "..."}
"""
import json
import os
import socket
import subprocess
import sys
import time

SOFFICE = os.environ.get("SOFFICE_BIN", r"C:\Program Files\LibreOffice\program\soffice.exe")
HOST = "127.0.0.1"
PORT = 2002


def _port_open(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _ensure_listener():
    if _port_open(HOST, PORT):
        return
    subprocess.Popen(
        [
            SOFFICE,
            "--headless",
            "--invisible",
            "--nologo",
            "--norestore",
            f"--accept=socket,host={HOST},port={PORT};urp;",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        if _port_open(HOST, PORT):
            return
        time.sleep(0.5)
    raise RuntimeError("LibreOffice headless listener did not start in time")


def _set_author(smgr, ctx, author):
    from com.sun.star.beans import PropertyValue

    given, _, family = author.partition(" ")
    pv = PropertyValue()
    pv.Name = "nodepath"
    pv.Value = "/org.openoffice.UserProfile/Data"
    cfg = smgr.createInstanceWithContext(
        "com.sun.star.configuration.ConfigurationProvider", ctx
    ).createInstanceWithArguments(
        "com.sun.star.configuration.ConfigurationUpdateAccess", (pv,)
    )
    cfg.setPropertyValue("givenname", given)
    cfg.setPropertyValue("sn", family)
    cfg.commitChanges()


def run(params):
    import uno
    from com.sun.star.beans import PropertyValue

    doc_path = params["path"]
    anchor_text = params["anchor_text"]
    new_text = params["new_text"]
    author = params.get("author")
    insert_mode = params.get("insert_mode", "after")

    _ensure_listener()

    local_ctx = uno.getComponentContext()
    resolver = local_ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_ctx
    )

    ctx = None
    last_err = None
    for _ in range(40):
        try:
            ctx = resolver.resolve(
                f"uno:socket,host={HOST},port={PORT};urp;StarOffice.ComponentContext"
            )
            break
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    if ctx is None:
        raise RuntimeError(f"No se pudo conectar a LibreOffice: {last_err}")

    smgr = ctx.ServiceManager

    if author:
        _set_author(smgr, ctx, author)

    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    hidden = PropertyValue()
    hidden.Name = "Hidden"
    hidden.Value = True

    url = uno.systemPathToFileUrl(doc_path)
    doc = desktop.loadComponentFromURL(url, "_blank", 0, (hidden,))

    try:
        doc.setPropertyValue("RecordChanges", True)

        text = doc.getText()
        search = doc.createSearchDescriptor()
        search.SearchString = anchor_text
        found = doc.findFirst(search)
        if found is None:
            raise RuntimeError(f"Texto ancla no encontrado: {anchor_text!r}")

        if insert_mode == "before":
            cursor = text.createTextCursorByRange(found.getStart())
            insertion = new_text + "\n"
        else:
            cursor = text.createTextCursorByRange(found.getEnd())
            insertion = "\n" + new_text

        text.insertString(cursor, insertion, False)
        doc.store()
    finally:
        doc.close(False)


def main():
    params_path, result_path = sys.argv[1], sys.argv[2]
    with open(params_path, "r", encoding="utf-8") as f:
        params = json.load(f)

    result = {"success": True}
    try:
        run(params)
    except Exception as e:
        result = {"success": False, "error": str(e)}

    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f)


if __name__ == "__main__":
    main()
