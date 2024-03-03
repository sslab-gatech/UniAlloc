import sys
import math

if __name__ == '__main__':
    file_loc = sys.argv[1]
    with open(file_loc, 'r')as fp:
        lines = fp.readlines()
        counter = 0
        total = 0
        for i in range(1, len(lines)):
            ls = lines[i].split(' ')
            if len(ls) > 1:
                last = float(ls[-2])
                if math.isinf(last) or math.isnan(last) or last == 0:
                    print("[{}] isn't legal, ignore it".format(lines[i][:-1]))
                else:
                    counter += 1
                    total += 1.0 / last
        print("Total {} benches with average result {}".format(counter, counter / total))

