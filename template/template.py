from e2b import Template

template = (
    Template()
    .from_node_image("24")
    .apt_install(["curl", "git", "ripgrep"])
    .copy(".mcp.json", "/.mcp/mcp.json")
    # Claude Code will be available globally as "claude"
    .npm_install("-g @anthropic-ai/claude-code@latest")
)
