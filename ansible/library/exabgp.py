#!/usr/bin/python

from ansible.module_utils.basic import AnsibleModule
import jinja2
import sys
import os
import re
import time
import six

DOCUMENTATION = '''
module:  exabgp
version_added:  "1.0"

short_description: manage exabgp instance
description: start/stop exabgp instance with certain configurations

Options:
    - option-name: name
      description: exabgp instance name
      required: True
    - option-name: state
      description: instance state. [started|stopped|present|absent]
      required: True

'''

EXAMPLES = '''
- name: start exabgp
  exabgp:
    name: t1
    state: started
    router_id: 10.0.0.0
    local_ip: 10.0.0.0
    peer_ip: 10.0.0.1
    local_asn: 65534
    peer_asn: 65535
    port: 5000

- name: stop exabgp
  exabgp:
    name: t1
    state: stopped
'''


DEFAULT_BGP_LISTEN_PORT = 179

http_api_py = '''\
from __future__ import print_function
import tornado.ioloop
import tornado.web
import sys

class route_handler(tornado.web.RequestHandler):
    def post(self):
        # Read the form data
        command = self.get_body_argument("command", None)
        commands = self.get_body_argument("commands", None)

        # Process and print the command values
        if command:
            out_str = "{}\\n".format(command)
            sys.stdout.write(out_str)
        if commands:
            values = commands.split(';')
            for value in values:
                out_str = "{}\\n".format(value)
                sys.stdout.write(out_str)

        sys.stdout.flush()
        self.write("OK\\n")

def make_app():
    return tornado.web.Application([
        (r"/upload", UploadHandler),
    ])

if __name__ == "__main__":
    app = tornado.web.Application([
        ("/", route_handler),
    ])
    app.listen(int(sys.argv[1]))
    tornado.ioloop.IOLoop.current().start()
'''

exabgp3_dump_config_tmpl = '''\
    process dump {
        run /usr/bin/python {{ dump_script }};
        encoder json;
        receive {
            parsed;
            update;
        }
    }
'''

# ExaBGP Version 3 configuration file format
exabgp3_config_template = '''\
group exabgp {
{{ dump_config }}

    process http-api {
        run /usr/bin/python /usr/share/exabgp/http_api.py {{ port }};
    }

    neighbor {{ peer_ip }} {
        router-id {{ router_id }};
        local-address {{ local_ip }};
        peer-as {{ peer_asn }};
        local-as {{ local_asn }};
        auto-flush {{ auto_flush }};
        group-updates {{ group_updates }};
        {%- if passive %}
        passive;
        listen {{ listen_port }};
        {%- endif %}
    }
}
'''

# ExaBGP Version 4 uses a different configuration file
# format. The dump_config would come from the user. The caller
# must pass Version 4 compatible configuration.
# Example configs are available here
# https://github.com/Exa-Networks/exabgp/tree/master/etc/exabgp
# Look for sample for a given section for details

exabgp4_dump_config_tmpl = '''\
    process dump {
        run /usr/bin/python {{ dump_script }};
        encoder json;
    }
'''

exabgp4_config_template = '''\
{{ dump_config }}
process http-api {
   run /usr/bin/python /usr/share/exabgp/http_api.py {{ port }};
   encoder json;
}
neighbor {{ peer_ip }} {
   router-id {{ router_id }};
   local-address {{ local_ip }};
   peer-as {{ peer_asn }};
   local-as {{ local_asn }};
   auto-flush {{ auto_flush }};
   group-updates {{ group_updates }};
   {%- if passive %}
   passive;
   listen {{ listen_port }};
   {%- endif %}
   api http_api{
       processes [ http-api ];
   }
   {%- if dump_config %}
   api dumper {
       processes [ dump ];
       receive {
           parsed;
           update;
       }
   }
   {%- endif %}
}
'''

# Unlike in ExaBGP V3.x, in V4+ the process API is expected to acknowledge
# with 'done' or 'error' string back to ExaBGP. Else the pipe becomes blocked
# and ExaBGP will hang. Alternatively the acknowledgement can be disabled.
# https://github.com/Exa-Networks/exabgp/wiki/Migration-from-3.4-to-4.x#api
exabgp_v4_env_tmpl = '''\
[exabgp.api]
ack = false
'''

exabgp_supervisord_conf_tmpl_p1 = '''\
[program:exabgp-{{ name }}]
'''
exabgp_supervisord_conf_tmpl_p3 = '''\
stdout_logfile=/tmp/exabgp-{{ name }}.out.log
stderr_logfile=/tmp/exabgp-{{ name }}.err.log
stdout_logfile_maxbytes=10000000
stdout_logfile_backups=2
stderr_logfile_maxbytes=10000000
stderr_logfile_backups=2
redirect_stderr=false
autostart=true
autorestart=true
startsecs=1
numprocs=1
'''
exabgp_supervisord_conf_tmpl_p2_v3 = '''\
command=/usr/local/bin/exabgp /etc/exabgp/{{ name }}.conf
'''
exabgp_supervisord_conf_tmpl_p2_v3_debug = '''\
command=/usr/local/bin/exabgp --debug /etc/exabgp/{{ name }}.conf
'''
exabgp_supervisord_conf_tmpl_p2_v4 = '''\
command=/usr/local/bin/exabgp --env /etc/exabgp/exabgp.env /etc/exabgp/{{ name }}.conf
'''
exabgp_supervisord_conf_tmpl_p2_v4_debug = '''\
command=/usr/local/bin/exabgp --debug --env /etc/exabgp/exabgp.env /etc/exabgp/{{ name }}.conf
'''


def exec_command(module, cmd, ignore_error=False, msg="executing command"):
    rc, out, err = module.run_command(cmd)
    if not ignore_error and rc != 0:
        module.fail_json(msg="Failed %s: rc=%d, out=%s, err=%s" %
                         (msg, rc, out, err))
    return out


def get_exabgp_status(module, name):
    output = exec_command(module, cmd="supervisorctl status exabgp-%s" % name)
    m = None
    if six.PY2:
        m = re.search(r'^([\w|-]*)\s+(\w*).*$', output.decode("utf-8"))
    else:
        # For PY3 module.run_command encoding is "utf-8" by default
        m = re.search(r'^([\w|-]*)\s+(\w*).*$', output)
    return m.group(2)


def refresh_supervisord(module):
    exec_command(module, cmd="supervisorctl reread", ignore_error=True)
    exec_command(module, cmd="supervisorctl update", ignore_error=True)


def start_exabgp(module, name):
    refresh_supervisord(module)
    exec_command(module, cmd="supervisorctl start exabgp-%s" % name)

    for count in range(0, 60):
        time.sleep(1)
        status = get_exabgp_status(module, name)
        if u'RUNNING' == status:
            break
    assert u'RUNNING' == status


def restart_exabgp(module, name):
    refresh_supervisord(module)
    exec_command(module, cmd="supervisorctl restart exabgp-%s" % name)

    for count in range(0, 60):
        time.sleep(1)
        status = get_exabgp_status(module, name)
        if u'RUNNING' == status:
            break
    assert u'RUNNING' == status


def stop_exabgp(module, name):
    exec_command(module, cmd="supervisorctl stop exabgp-%s" %
                 name, ignore_error=True)


def setup_exabgp_conf(name, router_id, local_ip, peer_ip, local_asn, peer_asn, port,
                      auto_flush=True, group_updates=True, dump_script=None, passive=False):
    try:
        os.mkdir("/etc/exabgp", 0o755)
    except OSError:
        pass

    dump_config = ""
    if dump_script:
        if six.PY2:
            dump_config = jinja2.Template(
                exabgp3_dump_config_tmpl).render(dump_script=dump_script)
        else:
            dump_config = jinja2.Template(
                exabgp4_dump_config_tmpl).render(dump_script=dump_script)

    # backport friendly checking; not required if everything is Py3
    t = None
    if six.PY2:
        t = jinja2.Template(exabgp3_config_template)
    else:
        t = jinja2.Template(exabgp4_config_template)
    data = t.render(name=name,
                    router_id=router_id,
                    local_ip=local_ip,
                    peer_ip=peer_ip,
                    local_asn=local_asn,
                    peer_asn=peer_asn,
                    port=port,
                    auto_flush=auto_flush,
                    group_updates=group_updates,
                    dump_config=dump_config,
                    passive=passive,
                    listen_port=DEFAULT_BGP_LISTEN_PORT)
    with open("/etc/exabgp/%s.conf" % name, 'w') as out_file:
        out_file.write(data)


def setup_exabgp_env():
    try:
        os.mkdir("/etc/exabgp", 0o755)
    except OSError:
        pass
    with open("/etc/exabgp/exabgp.env", 'w') as out_file:
        out_file.write(exabgp_v4_env_tmpl)


def remove_exabgp_conf(name):
    try:
        os.remove("/etc/exabgp/%s.conf" % name)
    except Exception:
        pass


def setup_exabgp_supervisord_conf(name, debug=False):
    exabgp_supervisord_conf_tmpl = None
    if six.PY2:
        if debug:
            exabgp_supervisord_conf_tmpl = exabgp_supervisord_conf_tmpl_p1 + \
                exabgp_supervisord_conf_tmpl_p2_v3_debug + \
                exabgp_supervisord_conf_tmpl_p3
        else:
            exabgp_supervisord_conf_tmpl = exabgp_supervisord_conf_tmpl_p1 + \
                exabgp_supervisord_conf_tmpl_p2_v3 + \
                exabgp_supervisord_conf_tmpl_p3
    else:
        if debug:
            exabgp_supervisord_conf_tmpl = exabgp_supervisord_conf_tmpl_p1 + \
                exabgp_supervisord_conf_tmpl_p2_v4_debug + \
                exabgp_supervisord_conf_tmpl_p3
        else:
            exabgp_supervisord_conf_tmpl = exabgp_supervisord_conf_tmpl_p1 + \
                exabgp_supervisord_conf_tmpl_p2_v4 + \
                exabgp_supervisord_conf_tmpl_p3
    t = jinja2.Template(exabgp_supervisord_conf_tmpl)
    data = t.render(name=name)
    with open("/etc/supervisor/conf.d/exabgp-%s.conf" % name, 'w') as out_file:
        out_file.write(data)


def remove_exabgp_supervisord_conf(name):
    try:
        os.remove("/etc/supervisor/conf.d/exabgp-%s.conf" % name)
    except Exception:
        pass


def setup_exabgp_processor():
    try:
        os.mkdir("/usr/share/exabgp", 0o755)
    except OSError:
        pass
    with open("/usr/share/exabgp/http_api.py", 'w') as out_file:
        out_file.write(http_api_py)


def main():
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(required=True, type='str'),
            state=dict(required=True, choices=[
                       'started', 'restarted', 'stopped', 'present', 'absent', 'status', 'configure'], type='str'),
            router_id=dict(required=False, type='str'),
            local_ip=dict(required=False, type='str'),
            peer_ip=dict(required=False, type='str'),
            local_asn=dict(required=False, type='int'),
            peer_asn=dict(required=False, type='int'),
            port=dict(required=False, type='int', default=5000),
            dump_script=dict(required=False, type='str', default=None),
            passive=dict(required=False, type='bool', default=False),
            debug=dict(required=False, type='bool', default=False)
        ),
        supports_check_mode=False)

    name = module.params['name']
    state = module.params['state']
    router_id = module.params['router_id']
    local_ip = module.params['local_ip']
    peer_ip = module.params['peer_ip']
    local_asn = module.params['local_asn']
    peer_asn = module.params['peer_asn']
    port = module.params['port']
    dump_script = module.params['dump_script']
    passive = module.params['passive']
    debug = module.params['debug']

    setup_exabgp_processor()
    if not six.PY2:
        setup_exabgp_env()

    result = {}
    try:
        if state == 'started':
            setup_exabgp_conf(name, router_id, local_ip, peer_ip, local_asn,
                              peer_asn, port, dump_script=dump_script, passive=passive)
            setup_exabgp_supervisord_conf(name, debug=debug)
            refresh_supervisord(module)
            start_exabgp(module, name)
        elif state == 'restarted':
            setup_exabgp_conf(name, router_id, local_ip, peer_ip, local_asn,
                              peer_asn, port, dump_script=dump_script, passive=passive)
            setup_exabgp_supervisord_conf(name, debug=debug)
            refresh_supervisord(module)
            restart_exabgp(module, name)
        elif state == 'present':
            setup_exabgp_conf(name, router_id, local_ip, peer_ip, local_asn,
                              peer_asn, port, dump_script=dump_script, passive=passive)
            setup_exabgp_supervisord_conf(name, debug=debug)
            refresh_supervisord(module)
        elif state == 'configure':
            setup_exabgp_conf(name, router_id, local_ip, peer_ip, local_asn,
                              peer_asn, port, dump_script=dump_script, passive=passive)
            setup_exabgp_supervisord_conf(name, debug=debug)
        elif state == 'stopped':
            stop_exabgp(module, name)
        elif state == 'absent':
            stop_exabgp(module, name)
            remove_exabgp_supervisord_conf(name)
            remove_exabgp_conf(name)
            refresh_supervisord(module)
        elif state == 'status':
            status = get_exabgp_status(module, name)
            result = {'status': status}
    except Exception:
        err = str(sys.exc_info())
        module.fail_json(msg="Error: %s" % err)

    module.exit_json(**result)


if __name__ == '__main__':
    main()
