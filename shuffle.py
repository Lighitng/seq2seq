import os
import random

def shuffle_file():
  lines = []
  f = open('./data/zh.zh', 'r', encoding='utf8')
  lines1 = f.readlines()
  f.close()
# drive path /content/gdrive/My Drive/mycode/bert
  f = open('./data/en.en', 'r', encoding='utf8')
  lines2 = f.readlines()
  all = []
  for i in range(len(lines1)):
    all.append([lines1[i], lines2[i]])
  random.shuffle(all)
  f.close()

  len_dat = len(lines1)
  ratio = 0.7

  val_num = int(len_dat * (1 - ratio))
  # zh val
  f = open(os.path.abspath('./data') + '/valid.zh', 'w', encoding='utf8')
  lines = lines1[0:val_num]
  f.writelines(lines)
  f.close()
  # en val
  f = open(os.path.abspath('./data') + '/valid.en', 'w', encoding='utf8')
  lines = lines2[0:val_num]
  f.writelines(lines)
  f.close()
  # zh train
  f = open(os.path.abspath('./data') + '/train.zh', 'w', encoding='utf8')
  lines = lines1[val_num:len_dat]
  f.writelines(lines)
  f.close()
  # en train
  f = open(os.path.abspath('./data') + '/train.en', 'w', encoding='utf8')
  lines = lines2[val_num:len_dat]
  f.writelines(lines)
  f.close()

if __name__ == "__main__":
  shuffle_file()
