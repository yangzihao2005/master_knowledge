import fitz, os
from PIL import Image
src = r'c:\Users\Yzh_laptop\Desktop\27张宇基础30讲（高数）.pdf'
doc = fitz.open(src)
res = []
for i in range(doc.page_count):
    pix = doc[i].get_pixmap(matrix=fitz.Matrix(0.2, 0.2))
    im = Image.frombytes('RGB', (pix.width, pix.height), pix.samples).convert('RGB')
    w, h = im.size
    px = im.load()
    nonwhite = 0
    cyan = 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            r, g, b = px[x, y]
            if r < 240 or g < 240 or b < 240:
                nonwhite += 1
                if b > 150 and g > 120 and r < 120:
                    cyan += 1
    tot = (w // 2) * (h // 2)
    res.append((i + 1, nonwhite / tot, cyan / tot))
lines = ['page nonwhite cyan']
for p, n, c in res:
    lines.append(f'{p}\t{n:.3f}\t{c:.3f}')
open(r'd:\master_knowledge\scripts\_probe2.txt', 'w', encoding='utf-8').write('\n'.join(lines))
cand = [p for p, n, c in res if n < 0.12 and c > 0.005]
print('candidates', len(cand))
print(cand)
