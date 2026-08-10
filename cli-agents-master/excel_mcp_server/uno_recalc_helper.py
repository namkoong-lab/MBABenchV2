#!/usr/bin/python3
"""
UNO Recalculation Helper

Standalone script that connects to a running soffice instance via UNO,
opens an Excel file, forces full recalculation, saves, and closes.

Must be run with the SYSTEM Python that has python3-uno installed.
Usage:
    /usr/bin/python3 uno_recalc_helper.py --ping <port>
    /usr/bin/python3 uno_recalc_helper.py --recalc <port> <file_path>
"""
import sys
import os


def connect_to_soffice(port):
    """Connect to running soffice via UNO socket."""
    import uno

    local_context = uno.getComponentContext()
    resolver = local_context.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_context
    )
    ctx = resolver.resolve(
        f"uno:socket,host=localhost,port={port};urp;StarOffice.ComponentContext"
    )
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    return desktop


def ping(port):
    """Test UNO connectivity."""
    try:
        desktop = connect_to_soffice(port)
        if desktop:
            print("OK")
            return 0
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
    return 1


def recalculate(port, file_path):
    """Open file, recalculate all formulas, save, close."""
    import uno
    from com.sun.star.beans import PropertyValue

    abs_path = os.path.abspath(file_path)
    file_url = "file://" + abs_path

    desktop = connect_to_soffice(port)

    # Open the document hidden
    open_props = []

    p_hidden = PropertyValue()
    p_hidden.Name = "Hidden"
    p_hidden.Value = True
    open_props.append(p_hidden)

    p_macro = PropertyValue()
    p_macro.Name = "MacroExecutionMode"
    p_macro.Value = 4  # ALWAYS_EXECUTE_NO_WARN
    open_props.append(p_macro)

    doc = desktop.loadComponentFromURL(file_url, "_blank", 0, tuple(open_props))
    if not doc:
        print("Failed to open document", file=sys.stderr)
        return 1

    try:
        # Force full recalculation of all formulas
        doc.calculateAll()

        # Save in place (keeps xlsx format with cached values)
        doc.store()
        return 0
    except Exception as e:
        print(f"Recalculation error: {e}", file=sys.stderr)
        return 1
    finally:
        doc.close(True)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: uno_recalc_helper.py --ping <port>")
        print("       uno_recalc_helper.py --recalc <port> <file_path>")
        sys.exit(1)

    command = sys.argv[1]
    port = int(sys.argv[2])

    if command == "--ping":
        sys.exit(ping(port))
    elif command == "--recalc":
        if len(sys.argv) < 4:
            print("Missing file_path argument", file=sys.stderr)
            sys.exit(1)
        sys.exit(recalculate(port, sys.argv[3]))
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)
