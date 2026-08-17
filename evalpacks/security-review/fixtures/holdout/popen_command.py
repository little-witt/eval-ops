import os


def archive(name):
    return os.popen("tar -czf /tmp/out.tgz " + name).read()
