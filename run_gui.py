"""Launch native Windows HYSPLIT GUI (Linux, macOS, WSL prohibited)."""

from hysplit_tool.platform_guard import require_native_windows

if __name__ == "__main__":
    require_native_windows()
    from hysplit_tool.gui import main
    main()
