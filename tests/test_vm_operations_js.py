"""Run the real My Labs script under Node and observe what Extend posts.

The /extend route can only extend the deployment the browser names, so the
request body is the contract (ops#44). The harness stubs just enough DOM for
static/js/utils.js and static/js/vm-operations.js to render a /list payload,
then clicks each rendered Extend button and records the form it submits.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [utilsPath, opsPath, payloadJson] = process.argv.slice(1);

const elements = {};
function element(id) {
  if (!elements[id]) {
    elements[id] = { id, innerHTML: '', value: '', addEventListener() {},
                     querySelector() { return null; } };
  }
  return elements[id];
}

const unescape = (s) => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'")
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

// Rendered buttons, rebuilt from the markup the script wrote.
function buttons(className) {
  const html = element('my-labs-content').innerHTML;
  const found = [];
  const tag = new RegExp('<button[^>]*class="[^"]*\\b' + className + '\\b[^"]*"[^>]*>', 'g');
  for (const match of html.matchAll(tag)) {
    const dataset = {};
    for (const attr of match[0].matchAll(/data-([a-z-]+)="([^"]*)"/g)) {
      const key = attr[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      dataset[key] = unescape(attr[2]);
    }
    const button = { dataset, disabled: false, innerHTML: '', handlers: [],
                     addEventListener(type, fn) { if (type === 'click') this.handlers.push(fn); } };
    found.push(button);
  }
  return found;
}

const rendered = {};
const posts = [];
const context = {
  console, setTimeout() {}, setInterval() { return 0; }, clearInterval() {},
  confirm: () => true, alert() {}, FormData, URLSearchParams,
  navigator: {},
  document: {
    getElementById: element,
    addEventListener() {},
    querySelectorAll(selector) {
      const name = selector.replace(/^\./, '');
      if (!rendered[name]) rendered[name] = buttons(name);
      return rendered[name];
    },
  },
  fetch(url, options) {
    if (url === '/list') {
      return Promise.resolve({ json: () => Promise.resolve(JSON.parse(payloadJson)) });
    }
    posts.push({ url, form: Object.fromEntries(options.body.entries()) });
    return new Promise(() => {});
  },
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(utilsPath, 'utf8'), context);
vm.runInContext(fs.readFileSync(opsPath, 'utf8'), context);
vm.runInContext('loadVMList()', context);

setImmediate(() => setImmediate(() => {
  for (const button of context.document.querySelectorAll('.extend-lab-btn')) {
    for (const handler of button.handlers) handler.call(button);
  }
  process.stdout.write(JSON.stringify(posts));
}));
"""


def _card(scope, *, student_name):
    return {
        'student_name': student_name,
        'created_by_user_id': 1,
        'deployment_groups': [{
            'group_name': 'homework1',
            'deployment_scope': scope,
            'vm_count': 1,
            'time_remaining_seconds': 600,
            'vms': [],
        }],
    }


def _extend_posts(payload):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is required to execute static/js')
    result = subprocess.run(
        [node, '-e', HARNESS,
         str(ROOT / 'static/js/utils.js'),
         str(ROOT / 'static/js/vm-operations.js'),
         json.dumps(payload)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


def test_each_extend_button_posts_its_own_deployment_scope():
    payload = {'success': True, 'students': [
        _card('individual', student_name='alice'),
        _card('group', student_name='team1'),
    ]}

    posts = _extend_posts(payload)

    assert [post['url'] for post in posts] == ['/extend', '/extend']
    assert [(post['form']['deployment_group'],
             post['form']['student_name'],
             post['form'].get('deployment_scope')) for post in posts] == [
        ('homework1', 'alice', 'individual'),
        ('homework1', 'team1', 'group'),
    ]
