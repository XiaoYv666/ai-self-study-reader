"""重新生成前端 mock PDF（旧版手写 PDF 语法非法，pdf.js 报 Invalid Root reference）。
用 PyMuPDF 生成合法文件，页数与 mockData.ts 严格对齐。"""
import pymupdf

PAGES = {'green': 14, 'taylor': 12, 'eigen': 16, 'vspace': 10, 'emag': 20, 'thermo': 15}
TITLES = {
    'green': '格林公式与曲线积分', 'taylor': '泰勒展开与中值定理',
    'eigen': '特征值与对角化', 'vspace': '向量空间与线性变换',
    'emag': '电磁感应与麦克斯韦方程组', 'thermo': '热力学基础',
}
SUB = {
    'green': 'Green Formula & Line Integrals', 'taylor': "Taylor Expansion & Mean Value Theorems",
    'eigen': 'Eigenvalues & Diagonalization', 'vspace': 'Vector Spaces & Linear Maps',
    'emag': 'Electromagnetic Induction & Maxwell', 'thermo': 'Thermodynamics Basics',
}

for key, n in PAGES.items():
    path = f'frontend/public/mock/{key}.pdf'
    doc = pymupdf.open()
    for i in range(n):
        p = doc.new_page(width=960, height=540)
        p.insert_text((60, 110), TITLES[key], fontsize=30, fontname='china-s')
        p.insert_text((60, 160), SUB[key], fontsize=14, fontname='helv')
        p.insert_text((60, 260), f'Page {i + 1} / {n}', fontsize=22, fontname='helv')
        p.draw_rect(pymupdf.Rect(40, 40, 920, 500), color=(0.64, 0.41, 0.23), width=1.5)
    doc.save(path)
    c = pymupdf.open(path)
    assert c.page_count == n, f'{path} 页数错: {c.page_count} != {n}'
    _ = c[0].get_text()
    c.close()
    print(f'OK {key}.pdf {n}页')
print('全部校验通过')
