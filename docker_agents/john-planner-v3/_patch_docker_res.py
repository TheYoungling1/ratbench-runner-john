import json, re

with open('multi_docker_eval_output/docker_res.json') as f:
    d = json.load(f)

with open('single.jsonl') as f:
    instance = json.loads(f.read())

test_patch = instance.get('test_patch', '')
instance_id = 'getlogbook__logbook-183'
entry = d[instance_id]

# 修正 eval_script: /testbed 路径
new_eval = (
    "#!/bin/bash\n\n"
    "cd /testbed\n\n"
    "python -m pytest tests/test_mail_handler.py::test_mail_handler_arguments -v\n"
    "TEST_EXIT_CODE=$?\n\n"
    'echo "echo OMNIGRIL_EXIT_CODE=$TEST_EXIT_CODE"\n'
    "exit $TEST_EXIT_CODE\n"
)
entry['eval_script'] = new_eval
entry['setup_scripts'] = {'test.patch': test_patch}

# 把 Dockerfile 中的 /app 全替换为 /testbed
old_df = entry['dockerfile']
new_df = old_df.replace('WORKDIR /app', 'WORKDIR /testbed').replace('/app', '/testbed')
entry['dockerfile'] = new_df

with open('multi_docker_eval_output/docker_res.json', 'w') as f:
    json.dump(d, f, indent=2)
print('Done')
print('Dockerfile:')
print(entry['dockerfile'])
