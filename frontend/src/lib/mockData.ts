// 内置 mock 数据：后端未启动时前端自动降级演示（接口形态与后端约定一致）
import type { Subject, TreeResponse, DocumentNode } from '../types'

export const MOCK_SUBJECTS: Subject[] = [
  { id: 's01', name: '高等数学', doc_count: 2 },
  { id: 's02', name: '线性代数', doc_count: 2 },
  { id: 's03', name: '大学物理', doc_count: 2 },
]

const doc = (id: string, filename: string, page_count: number, folder_id: string | null = null): DocumentNode => ({
  id,
  filename,
  page_count,
  folder_id,
})

export const MOCK_TREES: Record<string, TreeResponse> = {
  s01: {
    folders: [
      { id: 'f01', name: '第十章 · 曲线积分', parent_id: null },
    ],
    documents: [
      doc('d-green', '格林公式与曲线积分.pdf', 14, 'f01'),
      doc('d-taylor', '泰勒展开与中值定理.pdf', 12),
    ],
  },
  s02: {
    folders: [
      { id: 'f11', name: '第二章 · 矩阵', parent_id: null },
      { id: 'f12', name: '第五章 · 特征值', parent_id: 'f11' },
    ],
    documents: [
      doc('d-eigen', '特征值与对角化.pdf', 16, 'f12'),
      doc('d-vspace', '向量空间与线性变换.pdf', 10),
    ],
  },
  s03: {
    folders: [],
    documents: [
      doc('d-emag', '电磁感应与麦克斯韦方程组.pdf', 20),
      doc('d-thermo', '热力学基础.pdf', 15),
    ],
  },
}

/** 每个 mock 课件对应的 PDF 静态文件（public/mock/ 下，见 scripts/gen_mock_pdfs.py） */
export const MOCK_PDF_BY_DOC: Record<string, string> = {
  'd-green': '/mock/green.pdf',
  'd-taylor': '/mock/taylor.pdf',
  'd-eigen': '/mock/eigen.pdf',
  'd-vspace': '/mock/vspace.pdf',
  'd-emag': '/mock/emag.pdf',
  'd-thermo': '/mock/thermo.pdf',
}

/** 中文名 → 内置 PDF 兜底（上传到 mock 科目的文件也映射到这份 PDF） */
export const FALLBACK_MOCK_PDF = '/mock/green.pdf'
