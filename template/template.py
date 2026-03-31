from e2b import Template

template = (
    Template()
    .from_node_image("24")
    .apt_install([
        "curl",
        "git",
        "ripgrep",
        "python3",
        "python3-pip",
        # Playwright Chromium system dependencies (headless shell)
        # These are the core libs Chromium needs on Debian/Ubuntu
        "libnss3",
        "libnspr4",
        "libatk1.0-0",
        "libatk-bridge2.0-0",
        "libcups2",
        "libdrm2",
        "libdbus-1-3",
        "libxkbcommon0",
        "libatspi2.0-0",
        "libxcomposite1",
        "libxdamage1",
        "libxfixes3",
        "libxrandr2",
        "libgbm1",
        "libpango-1.0-0",
        "libcairo2",
        "libasound2",
        "libwayland-client0",
    ])
    # Claude Code — the agent brain
    .npm_install("-g @anthropic-ai/claude-code@latest")
    # Playwright MCP server — browser automation via MCP protocol
    # This exposes browser tools (navigate, click, fill, screenshot, etc.)
    # that Claude Code can call through its native MCP integration
    .npm_install("-g @playwright/mcp@latest")
    # Install Chromium headless shell for Playwright
    # --only-shell skips full browser, saves ~200MB in template size
    .run_cmd("npx playwright install --with-deps chromium")
    # Also install Python Playwright for agents that want to write Python browser scripts
    .run_cmd("pip3 install playwright --break-system-packages")
    .run_cmd("python3 -m playwright install chromium")
)
