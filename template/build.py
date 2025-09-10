from .template import template
from e2b import Template

Template.build(
    template,
    alias="claude-code",
    cpu_count=2,
    memory_mb=4096,
    on_build_logs=lambda log_entry: print(log_entry),
)
