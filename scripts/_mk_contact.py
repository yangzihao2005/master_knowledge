import fitz, sys, os
from PIL import Image, ImageDraw

src = r'c:\Users\Yzh_laptop\Desktop\27张宇基础30讲（高数）.pdf'
outdir = r'C:\Users\Yzh_laptop\AppData\Local\Temp\opencode\zy_sheets'
os.makedirs(outdir, exist_ok=True)
doc = fitz.open(src)

start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
end = int(sys.argv[2]) if len(sys.argv) > 2 else 12
cols, rows = 4, 3
tw = 320
per = cols * rows
for s in range(start, end, per):
    pages = list(range(s, min(s + per, doc.page_count)))
    imgs = []
    for p in pages:
        pix = doc[p].get_pixmap(dpi=0, matrix=fitz.Matrix(0.35, 0.35))
        im = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        ratio = tw / im.width
        im = im.resize((tw, max(1, int(im.height * ratio))), Image.LANCZOS)
        imgs.append(im)
    th = max(i.height for i in imgs)
    sheet = Image.new('RGB', (cols * tw, rows * (th + 22)), 'white')
    d = ImageDraw.Draw(sheet)
    for k, im in enumerate(imgs):
        c, r = k % cols, k // cols
        x, y = c * tw, r * (th + 22)
        d.text((x + 4, y + 3), f'p{pages[k]+1}', fill='red')
        sheet.paste(im, (x, y + 20))
    sheet.save(os.path.join(outdir, f'sheet_{s+1:03d}.jpg'), quality=80)
    print(os.path.join(outdir, f'sheet_{s+1:03d}.jpg'))
print('done', doc.page_count)
