import 'dotenv/config'
import { Template, defaultBuildLogger } from 'e2b'
import { template as claudeCodeTemplate } from './template'

async function main() {
  console.log('Building E2B template: world-modal-agent-browser...')
  console.log('This template includes:')
  console.log('  - Claude Code CLI (latest)')
  console.log('  - Playwright MCP server (browser automation via MCP)')
  console.log('  - Chromium headless shell (for Playwright)')
  console.log('  - Python 3 + Playwright (for Python-based browser scripts)')
  console.log('')
  console.log('This will take 5-10 minutes due to Chromium download...')
  
  const tpl = await Template.build(claudeCodeTemplate, 'world-modal-agent-browser', {
    // Bumped resources for browser automation
    // Chromium needs more memory than text-only tasks
    cpuCount: 2,
    memoryMB: 4096,
    onBuildLogs: defaultBuildLogger(),
  })

  console.log(`\n✅ Template built successfully!`)
  console.log(`   Template ID: ${tpl.templateId}`)
  console.log(`   Name: world-modal-agent-browser`)
  console.log(`\nUpdate E2B_SANDBOX_TEMPLATE in Railway to: world-modal-agent-browser`)
}

main().catch((err) => {
  console.error('❌ Template build failed:', err)
  process.exit(1)
})
