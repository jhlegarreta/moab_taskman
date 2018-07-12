import json
import subprocess
import inspect
import time
import shutil
from datetime import datetime
from enum import Enum
from os import makedirs
from os.path import expandvars

HOMEDIR = expandvars('$HOME')
DB_STARTED_TASKS = HOMEDIR + '/taskman/started'


def fmt_time(seconds):
    if seconds >= 3600:
        return str(round(seconds / 3600)) + 'h'
    elif seconds >= 60:
        return str(round(seconds / 60)) + 'm'
    else:
        return str(round(seconds)) + 's'


class JobStatus(Enum):
    Dead = 'Dead'
    Finished = 'Finished'
    Unknown = '?'
    Running = 'Running'
    Waiting = 'Waiting'
    Blocked = 'Blocked'
    Lost = 'Lost'

    def __str__(self):
        return self.value

    @property
    def cancellable(self):
        return self in [JobStatus.Running, JobStatus.Waiting, JobStatus.Blocked]

    @property
    def needs_attention(self):
        return self in [JobStatus.Dead, JobStatus.Lost]


class Job(object):
    def __init__(self, task_id, name, moab_id, status, template_file, args_str):
        self.task_id = task_id
        self.moab_id = moab_id
        self.name = name
        self.status = status
        self.template_file = template_file
        self.args_str = args_str
        self.report = {}
        self.finish_msg = ''

    @property
    def script_file(self):
        _, script_file = Job.get_path(self.name, self.task_id)
        return script_file

    @staticmethod
    def get_path(task_name, task_id):
        script_path = HOMEDIR + '/script_moab/taskman/' + task_name
        script_file = script_path + '/' + task_id + '.sh'
        return script_path, script_file


class Taskman(object):
    jobs = {}
    columns = set()

    @staticmethod
    def get_cmd_output(args, timeout=20):
        try:
            output = subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.CalledProcessError as e:
            print('Error with command: ' + ' '.join(args))
            print(e.output)
            raise
        except subprocess.TimeoutExpired as e:
            print('Timeout with command: ' + ' '.join(args))
            print(e.output)
            return None
        return output.decode('UTF-8')

    @staticmethod
    def get_moab_queue():
        args = ['showq', '-w', expandvars('user=$USER'), '--blocking']
        output = Taskman.get_cmd_output(args, timeout=10)
        if output is None:
            return None, None, None

        showq_lines = output.split('\n')
        showq_lines = [l.strip() for l in showq_lines]
        lists = {'active j': [], 'eligible': [], 'blocked ': []}
        cur_list = None
        for line in showq_lines:
            if line[:8] in lists:
                cur_list = line[:8]
            elif line != '' and \
                            'JOBID' not in line and \
                            'processors' not in line and \
                            'nodes' not in line and \
                            'eligible' not in line and \
                            'Total' not in line and \
                            'blocked' not in line:
                moab_id = line.split(' ')[0]
                lists[cur_list].append(moab_id)
        return lists['active j'], lists['eligible'], lists['blocked ']

    @staticmethod
    def generate_script(job):
        script_path, script_file = Job.get_path(job.name, job.task_id)

        # Get template
        with open(HOMEDIR + '/script_moab/' + job.template_file + '.sh', 'r') as f:
            template = f.readlines()

        # Append post exec bash script
        with open(HOMEDIR + '/script_moab/taskman_post_exec.sh', 'r') as f:
            post_exec = f.readlines()
        template += post_exec

        # Replace variables
        script_lines = []
        for line in template:
            line = line.replace('$TASKMAN_NAME', job.name)
            line = line.replace('$TASKMAN_ID', job.task_id)
            line = line.replace('$TASKMAN_ARGS', job.args_str)
            script_lines.append(line)

        # Write script
        makedirs(script_path, exist_ok=True)
        with open(script_file, 'w') as f:
            f.writelines(script_lines)

        return script_file

    @staticmethod
    def create_task(template_file, args_str, task_name):
        # Generate id
        task_id = datetime.now().strftime("%m-%d_%H-%M-%S_%f")
        job = Job(task_id, task_name, None, None, template_file, args_str)
        script_file = Taskman.generate_script(job)

        print('Created', script_file)
        return job

    @staticmethod
    def write_started(job, db_file=None):
        if db_file is None:
            f = open(DB_STARTED_TASKS, 'a')
        else:
            f = db_file

        line = '{};{};{};{};{}'.format(job.task_id, job.name, job.moab_id, job.template_file, job.args_str)
        f.write(line + '\n')

        if db_file is None:
            f.close()

    @staticmethod
    def submit(job):
        # Submit using msub
        print('Calling msub...', end=' ')
        output = Taskman.get_cmd_output(['msub', job.script_file])
        if output is None:
            return

        # Get moab job id
        job.moab_id = output.strip()

        # Add to 'started' database
        Taskman.write_started(job)

        print('Submitted.  TaskmanID: {}  MoabID: {}'.format(job.task_id, job.moab_id))

    @staticmethod
    def cancel(task_id):
        job = Taskman.jobs[task_id]
        output = Taskman.get_cmd_output(['mjobctl', '-c', job.moab_id])
        if output is None:
            return

        # Add to 'finished' database
        with open(HOMEDIR + '/taskman/finished', 'a') as f:
            line = '{},{},{}'.format(job.moab_id, job.name, 'cancel')
            f.write(line + '\n')

        print(output.strip())

    @staticmethod
    def read_task_db():
        with open(HOMEDIR + '/taskman/started', 'r') as f:
            started_tasks_csv = f.readlines()
        with open(HOMEDIR + '/taskman/dead', 'r') as f:
            dead_tasks_csv = f.readlines()
        with open(HOMEDIR + '/taskman/finished', 'r') as f:
            finished_tasks_csv = f.readlines()

        started_tasks = {tokens[0]: tokens[1:] for tokens in [l.strip().split(';') for l in started_tasks_csv]}
        dead_tasks = {tokens[0]: tokens[1:] for tokens in [l.strip().split(',') for l in dead_tasks_csv]}
        finished_tasks = {tokens[0]: tokens[1:] for tokens in [l.strip().split(',') for l in finished_tasks_csv]}
        return started_tasks, dead_tasks, finished_tasks

    @staticmethod
    def update_job_list():
        active_jobs, eligible_jobs, blocked_jobs = Taskman.get_moab_queue()

        started_tasks, dead_tasks, finished_tasks = Taskman.read_task_db()

        jobs = {}
        for task_id, fields in sorted(started_tasks.items(), key=lambda x: x[1][0]):
            name, moab_id, template_file, args_str = fields
            j = Job(task_id, name, moab_id, None, template_file, args_str)

            if moab_id in dead_tasks:
                j.status = JobStatus.Dead
            elif moab_id in finished_tasks:
                j.status = JobStatus.Finished
                j.finish_msg = finished_tasks[moab_id][1]
            elif active_jobs is None:
                j.status = JobStatus.Unknown  # showq has timed out
            elif moab_id in active_jobs:
                j.status = JobStatus.Running
            elif moab_id in eligible_jobs:
                j.status = JobStatus.Waiting
            elif moab_id in blocked_jobs:
                j.status = JobStatus.Blocked
            else:
                j.status = JobStatus.Lost

            jobs[task_id] = j
        Taskman.jobs = jobs
        Taskman.update_report()

    @staticmethod
    def get_log(job, error_log=False):
        ext_prefix = '.e' if error_log else '.o'
        output_filepath = HOMEDIR + '/logs/' + job.name + ext_prefix + job.moab_id
        with open(output_filepath, 'r') as f:
            lines = f.readlines()
        return lines, output_filepath

    @staticmethod
    def update_report():
        Taskman.columns = set()
        for task_id, job in Taskman.jobs.items():
            report_line = None
            try:
                log_lines, _ = Taskman.get_log(job)
                for line in log_lines:
                    if line[:8] == '!taskman':
                        report_line = line
            except FileNotFoundError:
                pass
            if report_line is not None:
                job.report = json.loads(report_line[8:])
                Taskman.columns.update(job.report.keys())
        if 'time' in Taskman.columns:
            Taskman.columns.remove('time')

    @staticmethod
    def resume_incomplete_tasks():
        for task_id, job in Taskman.jobs.items():
            if job.status != JobStatus.Finished:
                continue
            do_resubmit = job.report.get('resubmit', False)
            if do_resubmit:
                Taskman.submit(job)
        time.sleep(2)

    @staticmethod
    def show_status():
        print('\033[2J\033[H')  # Clear screen and move cursor to top left
        print('\033[97;45m( Moab Task Manager )\033[0m     ' + time.strftime("%H:%M:%S"), end='')
        print('     \033[37mCtrl+C to enter command mode\033[0m')

        line_fmt = '{:<8} {:<30} {:<21} {:<7} {:<7}' + ' {:<12}' * len(Taskman.columns)
        print('\033[1m' + line_fmt.format('Status', 'Task name', 'Task id', 'Moab id', 'Updated',
                                          *sorted(Taskman.columns)) + '\033[0m')
        for task_id, job in sorted(Taskman.jobs.items(), key=lambda x: x[1].name):
            # Get report data
            report_columns = []
            for k in sorted(Taskman.columns):
                val_str = str(job.report.get(k, ''))[:12]
                report_columns.append(val_str)
            time_ago = fmt_time(time.time() - job.report['time']) if 'time' in job.report else ''
            # Format line
            status_line = line_fmt.format(job.status, job.name, task_id, job.moab_id, time_ago, *report_columns)
            if job.status.needs_attention:
                status_line = '\033[31m' + status_line + '\033[0m'
            elif job.status == JobStatus.Blocked:
                status_line = '\033[30;47m' + status_line + '\033[0m'
            elif job.status == JobStatus.Finished:
                finished_status = {'ok': '\033[32;107mFinished\033[;107m',  # Green
                                   'cancel': '\033[;107mCancel\'d'  # Black
                                   }.get(job.finish_msg, '\033[;107mFinished')
                status_line = finished_status + status_line[8:] + '\033[0m'
            print(status_line)

    @staticmethod
    def update(resume_incomplete_tasks=True):
        Taskman.update_job_list()
        Taskman.show_status()
        if resume_incomplete_tasks:
            Taskman.resume_incomplete_tasks()


def _handle_command(cmd_str):
    tokens = cmd_str.split(' ')
    cmd_name = tokens[0]
    if cmd_name == '':
        return
    if len(tokens) == 1:
        cmds[cmd_name]()
    else:
        cmd_args = ' '.join(tokens[1:])
        cmds[cmd_name](*cmd_args.split(';'))


def _show_commands():
    print('-------------------')
    print('Available commands:')
    for name, fn in sorted(cmds.items(), key=lambda x: x[0]):
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        print(name, ':', '; '.join([str(p) for p in params]))


def _match(pattern, name):
    if pattern.endswith('*'):
        return name.startswith(pattern[:-1])
    else:
        return name == pattern


def submit(template_file, args_str, task_name):
    job = Taskman.create_task(template_file, args_str, task_name)
    Taskman.submit(job)


def fromckpt(template_file, args_str, task_name, ckpt_file):
    job = Taskman.create_task(template_file, args_str, task_name)
    print('Moving checkpoint...')
    job_dir = expandvars('$SCKPT') + '/' + job.name + '/' + job.task_id
    makedirs(job_dir)
    shutil.move(HOMEDIR + '/' + ckpt_file, job_dir)
    Taskman.submit(job)


def multi_sub():
    print('Enter multiple submission lines. Add an empty line to end.')
    print()
    a = []
    while True:
        i = input()
        if i == '':
            break
        a.append(i)
    print('Tasks to submit:')
    for i in a:
        print(i)
    print()
    r = input('Submit? (y/n)')
    if r == 'y':
        for i in a:
            submit(*i.split(';'))


def continu(task_name):
    for task_id, job in Taskman.jobs.items():
        if job.status in [JobStatus.Finished, JobStatus.Dead, JobStatus.Lost] and _match(task_name, job.name):
            Taskman.submit(job)


def cancel(task_name):
    for task_id, job in Taskman.jobs.items():
        if job.status.cancellable and _match(task_name, job.name):
            Taskman.cancel(task_id)


def copy(task_name):
    submitted = set()
    for task_id, job in Taskman.jobs.items():
        if job.name not in submitted and _match(task_name, job.name):
            job = Taskman.create_task(job.template_file, job.args_str, job.name)
            Taskman.submit(job)
            submitted.add(job.name)


def show(task_name):
    print()
    for task_id, job in Taskman.jobs.items():
        if _match(task_name, job.name):
            out_log, out_log_file = Taskman.get_log(job)
            err_log, err_log_file = Taskman.get_log(job, error_log=True)

            print('\033[1m' + job.name + '\033[0m :', job.args_str)
            print('\033[30;44m' + ' ' * 40 + '\033[0m ' + out_log_file + '\r\033[2C Output ')
            for l in out_log[-20:]:
                print(l.strip())
            print('\033[30;44m' + ' ' * 40 + '\033[0m ' + err_log_file + '\r\033[2C Error ')
            for l in err_log[-30:]:
                print(l.strip())
            print('\033[30;44m' + ' ' * 40 + '\033[0m')
            print()
    input('Press any key...')


def pack(task_name):
    checkpoint_paths = []
    for task_id, job in Taskman.jobs.items():
        if job.status == JobStatus.Finished and _match(task_name, job.name):
            checkpoint_paths.append(job.name + '/' + job.task_id)
    # Call pack.sh
    subprocess.Popen([HOMEDIR + '/pack.sh'] + checkpoint_paths)


def _clean(task_name=None, clean_all=False):
    shutil.copyfile(DB_STARTED_TASKS,
                    HOMEDIR + '/taskman/old/started_' + datetime.now().strftime("%m-%d_%H-%M-%S"))

    started_tasks, dead_tasks, finished_tasks = Taskman.read_task_db()

    with open(DB_STARTED_TASKS, 'w') as f:
        for task_id, fields in started_tasks.items():
            name, moab_id, template_file, args_str = fields
            remove = clean_all or (moab_id in dead_tasks or moab_id in finished_tasks)
            if task_name is not None:
                remove = _match(task_name, name) and remove
            if not remove:
                job = Job(task_id, name, moab_id, None, template_file, args_str)
                Taskman.write_started(job, f)


def clean(task_name=None):
    _clean(task_name)


def cleanall(task_name=None):
    _clean(task_name, clean_all=True)


def regen_script(task_name):
    for task_id, job in Taskman.jobs.items():
        if _match(task_name, job.name):
            script = Taskman.generate_script(job)
            print('Regenerated', script)


# Available commands
cmds = {'sub': submit, 'fromckpt': fromckpt, 'multisub': multi_sub, 'cont': continu, 'cancel': cancel, 'copy': copy,
        'pack': pack, 'show': show, 'clean': clean, 'cleanall': cleanall, 'regen': regen_script}


if __name__ == '__main__':
    while True:
        command_mode = False
        try:
            Taskman.update()
            time.sleep(10)
        except KeyboardInterrupt:
            command_mode = True

        if command_mode:
            print('\rUpdating, please wait...')
            Taskman.update(resume_incomplete_tasks=False)
            _show_commands()
            command = input('\033[1mCommand>>\033[0m ')
            _handle_command(command)
