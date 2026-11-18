#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <dirent.h>
#include <ctype.h>
#include <limits.h>
/* Read the kernel's PSS/SwapPSS endpoints with PID identity around every read. */
static unsigned long long start(int pid) {
    char path[80], line[8192];
    snprintf(path,sizeof path,"/proc/%d/stat",pid);
    FILE *fp=fopen(path,"r");
    if (!fp) return 0;
    char *read=fgets(line,sizeof line,fp);
    fclose(fp);
    if (!read) return 0;
    char *end=strrchr(line,')'), *save=NULL;
    if (!end || end[1]!=' ') return 0;
    char *field=strtok_r(end+2," ",&save);
    for (int i=3; field && i<22; i++) field=strtok_r(NULL," ",&save);
    return field ? strtoull(field,NULL,10) : 0;
}
static int sample(int pid) {
    char path[80],line[256];
    unsigned long long first=start(pid);
    snprintf(path,sizeof path,"/proc/%d/smaps_rollup",pid);
    FILE *fp=fopen(path,"r");
    if (!fp) {
        fprintf(stderr,"cannot open %s\n",path);
        return 1;
    }
    long pss=-1,swap=-1;
    while (fgets(line,sizeof line,fp)) {
        long value;
        if (sscanf(line,"Pss: %ld kB",&value)==1) pss=value;
        if (sscanf(line,"SwapPss: %ld kB",&value)==1) swap=value;
    }
    int error=ferror(fp);
    fclose(fp);
    if (error || !first || first!=start(pid) || pss<0 || swap<0) {
        fprintf(stderr,"incomplete or changed process %d\n",pid);
        return 1;
    }
    printf("%d %llu %ld %ld\n",pid,first,pss,swap);
    return 0;
}
static int snapshot(int *pids, size_t n) {
    DIR *dir=opendir("/proc");
    if (!dir) return -1;
    size_t count=0;
    struct dirent *entry;
    while ((entry=readdir(dir))) {
        if (!isdigit((unsigned char)entry->d_name[0])) continue;
        char *end;
        long pid=strtol(entry->d_name,&end,10);
        if (*end || pid<=0 || pid>INT_MAX || pid==getpid()) continue;
        if (count==n) {closedir(dir);return -1;}
        pids[count++]=(int)pid;
    }
    closedir(dir);
    return (int)count;
}
static int cmp(const void *a,const void *b) {
    int x=*(const int *)a,y=*(const int *)b;
    return (x>y)-(x<y);
}
int main(int argc, char **argv) {
    int error=0;
    if (argc>1 && !strcmp(argv[1],"--guest")) {
        int before[4096],after[4096];
        int n=snapshot(before,4096);
        if (n<=0 || getpid()==1 || getppid()!=0 || !start(1)) return 2;
        for (int i=0;i<n;i++) error|=sample(before[i]);
        int m=snapshot(after,4096);
        if (m!=n) return 3;
        qsort(before,n,sizeof(int),cmp);
        qsort(after,m,sizeof(int),cmp);
        if (memcmp(before,after,n*sizeof(int))) return 3;
    } else {
        if (argc<2) return 2;
        for (int i=1;i<argc;i++) {
            char *end;
            long pid=strtol(argv[i],&end,10);
            if (*end || pid<=0 || pid>INT_MAX) return 2;
            error|=sample((int)pid);
        }
    }
    return error;
}
