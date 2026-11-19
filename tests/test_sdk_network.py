import os
import sys

import pytest

from snippet_sentinel.classifier import classify
from snippet_sentinel.extractor import Snippet
from snippet_sentinel.network_guard import MARKER
from snippet_sentinel.runner import command, run


@pytest.mark.parametrize('output,rc,expired,expected', [
    ('APIConnectionError: Connection error.', 1, False, 'needs_network'),
    ('StripeConnectionError: Connection failed', 1, False, 'needs_network'),
    ('TypeError: fetch failed', 1, False, 'needs_network'),
    ('', -9, True, 'needs_network'),
    ('caught', 0, False, 'needs_network'),
    ("AttributeError: object has no attribute 'renamed_method'", 1, False, 'runtime_error'),
    ('TypeError: client.renamed_method is not a function', 1, False, 'runtime_error'),
    ('SENTINEL_OUTPUT_LIMIT: output limit exceeded', -9, False, 'output_limit'),
])
def test_network_evidence(output, rc, expired, expected):
    assert classify(MARKER + '\n' + output, rc, expired, network_disabled=True) == expected
    if expired:
        assert classify(output, rc, expired, network_disabled=True) == 'timeout'


def test_network_evidence_survives_output_tail(tmp_path):
    out, rc, expired = command([sys.executable, '-c', f"print('{MARKER}',flush=True);print('x'*20000)"], tmp_path, 5)
    assert MARKER in out


@pytest.mark.parametrize('output', [
    "  client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])\nAttributeError: object has no attribute 'renamed_method'",
    "AuthenticationError: earlier error\n\nAttributeError: object has no attribute 'renamed_method'",
    "  process.env.API_KEY\nTypeError: client.renamed_method is not a function",
])
def test_terminal_programming_error_is_broken(output):
    assert classify(output, 1) == 'runtime_error'


@pytest.mark.parametrize('terminal', ['ZeroDivisionError: division by zero', 'ValueError: bad argument',
                                     'ValueError: Network is unreachable', 'RuntimeError: application failed',
                                     'RangeError: out of range'])
def test_caught_network_does_not_hide_terminal_failure(terminal):
    output = MARKER + '\nConnectionError: Network is unreachable\n\n' + terminal
    assert classify(output, 1, network_disabled=True) == 'runtime_error'


@pytest.mark.parametrize('terminal', ['APIConnectionError: Connection error.', 'httpx.ConnectError: failed',
                                     'socket.gaierror: [Errno -3] Network disabled by Snippet Sentinel',
                                     'OSError: [Errno 101] Network disabled by Snippet Sentinel',
                                     'AxiosError: Network disabled by Snippet Sentinel'])
def test_terminal_connection_failure_is_inconclusive(terminal):
    assert classify(MARKER + '\n' + terminal, 1, network_disabled=True) == 'needs_network'


CASES = [
    ('python', "from openai import OpenAI\nOpenAI().chat.completions.create(model='gpt-4o-mini',messages=[{'role':'user','content':'hello'}])", 'needs_credentials'),
    ('python', "import os\nos.environ['OPENAI_API_KEY']='sentinel-dummy-key'\nfrom openai import OpenAI\nOpenAI().chat.completions.create(model='gpt-4o-mini',messages=[{'role':'user','content':'hello'}])", 'needs_network'),
    ('python', "import anthropic\nanthropic.Anthropic(api_key='sentinel-dummy-key').messages.create(model='claude-sonnet-4-5',max_tokens=10,messages=[{'role':'user','content':'hello'}])", 'needs_network'),
    ('python', "import stripe\nstripe.api_key='sentinel-dummy-key'\nstripe.Customer.list(limit=1)", 'needs_network'),
    ('python', "import requests\nrequests.get('https://example.com')", 'needs_network'),
    ('python', "import httpx\nhttpx.get('https://example.com')", 'needs_network'),
    ('python', "import boto3\nboto3.client('s3',region_name='us-east-1',aws_access_key_id='sentinel-dummy-key',aws_secret_access_key='sentinel-dummy-secret').list_buckets()", 'needs_network'),
    ('python', "import os\nos.environ['AWS_EC2_METADATA_DISABLED']='true'\nimport boto3\nboto3.client('s3',region_name='us-east-1').list_buckets()", 'needs_credentials'),
    ('python', "from openai import OpenAI\nOpenAI(api_key='sentinel-dummy-key').chat.completions.renamed_method()", 'runtime_error'),
    ('javascript', "import OpenAI from 'openai';const client=new OpenAI();await client.chat.completions.create({model:'gpt-4o-mini',messages:[{role:'user',content:'hello'}]});", 'needs_credentials'),
    ('javascript', "import OpenAI from 'openai';const client=new OpenAI({apiKey:'sentinel-dummy-key'});await client.chat.completions.create({model:'gpt-4o-mini',messages:[{role:'user',content:'hello'}]});", 'needs_network'),
    ('javascript', "import Stripe from 'stripe';await new Stripe('sentinel-dummy-key').customers.list({limit:1});", 'needs_network'),
    ('javascript', "await fetch('https://example.com');", 'needs_network'),
    ('javascript', "import axios from 'axios';await axios.get('https://example.com');", 'needs_network'),
    ('python', "import socket,time\ntry: socket.create_connection(('example.com',443))\nexcept OSError: time.sleep(60)", 'needs_network'),
    ('javascript', "try{await fetch('https://example.com')}catch{};await new Promise(r=>setTimeout(r,60000));", 'needs_network'),
    ('python', "import socket\ntry: socket.create_connection(('example.com',443))\nexcept OSError: pass\nobject().renamed_method()", 'runtime_error'),
    ('python', "import socket\ntry: socket.create_connection(('example.com',443))\nexcept OSError: pass\n1/0", 'runtime_error'),
    ('python', "import socket\ntry: socket.create_connection(('example.com',443))\nexcept OSError: pass\nraise ValueError('bad input')", 'runtime_error'),
    ('javascript', "try{await fetch('https://example.com')}catch{};({}).renamed_method();", 'runtime_error'),
]


@pytest.mark.docker
@pytest.mark.skipif(not os.environ.get('SENTINEL_DOCKER_TESTS'), reason='Set SENTINEL_DOCKER_TESTS=1')
@pytest.mark.parametrize('lang,code,expected', CASES)
def test_sdk_offline(lang, code, expected):
    result = run(Snippet('sdk', 'sdk.md', 1, lang, code), timeout=4 if '60000' in code or 'sleep(60)' in code else 45)
    assert result['status'] == expected, result
