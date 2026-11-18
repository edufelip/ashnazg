#include <fcntl.h>
#include <linux/android/binderfs.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#define BINDERFS_ROOT "/dev/ashnazg-binderfs"

static int create_devices(int control) {
    static const char *names[] = {"binder", "hwbinder", "vndbinder"};
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        struct binderfs_device device = {0};
        char path[128], alias[128];
        snprintf(device.name, sizeof(device.name), "%s", names[i]);
        snprintf(path, sizeof(path), BINDERFS_ROOT "/%s", names[i]);
        snprintf(alias, sizeof(alias), "/dev/%s", names[i]);

        if ((access(path, F_OK) != 0 && ioctl(control, BINDER_CTL_ADD, &device) < 0)
            || chmod(path, 0666) != 0 || symlink(path, alias) != 0) {
            perror(path);
            return 1;
        }
    }
    return 0;
}

int main(int argc, char **argv) {
    if (getpid() != 1 || argc < 2 || strcmp(argv[1], "/init") != 0) {
        fprintf(stderr, "Binder bootstrap requires PID 1 and the pinned Android /init entry point\n");
        return 2;
    }
    if (mkdir(BINDERFS_ROOT, 0755) != 0
        || mount("binder", BINDERFS_ROOT, "binder", 0, NULL) != 0) {
        perror("mount private Binderfs");
        return 2;
    }

    int control = open(BINDERFS_ROOT "/binder-control", O_RDONLY | O_CLOEXEC);
    if (control < 0) {
        perror("open Binder control device");
        return 2;
    }

    int status = create_devices(control);
    close(control);
    if (status != 0) return 2;
    execv(argv[1], argv + 1);
    perror("exec Android init");
    return 2;
}
