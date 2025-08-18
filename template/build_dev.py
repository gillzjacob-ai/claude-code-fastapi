from .template import template
from e2b_template import Template

Template.build(
    template,
    alias="claude-code-dev",
    cpu_count=1,
    memory_mb=1024,
    on_build_logs=lambda log_entry: print(log_entry),
)
