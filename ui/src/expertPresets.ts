export interface ExpertPreset {
  label: string
  value: string
  questions: string[]
}

export const EXPERT_PRESETS: ExpertPreset[] = [
  {
    label: 'M&A contract review',
    value: 'ma-contract',
    questions: [
      'Identify any change-of-control clauses and summarize trigger conditions.',
      'What are the termination fee or break-up fee caps?',
      'Are there non-compete or non-solicitation obligations?',
      'Which court, jurisdiction, or governing law applies?',
    ],
  },
  {
    label: '10-K financial diagnosis',
    value: '10k-diagnosis',
    questions: [
      'What are the primary risk factors disclosed?',
      'What adjusted EBITDA or non-GAAP profitability metrics are disclosed?',
      'Are there material pending litigation matters?',
      'Are there related-party transactions or conflicts disclosed?',
    ],
  },
  {
    label: 'Commercial lease review',
    value: 'lease-review',
    questions: [
      'What rent-free period or tenant incentive terms are disclosed?',
      'What restrictions apply to assignment or subleasing?',
      'How are maintenance and repair responsibilities allocated?',
      'What renewal, termination, or default remedies are disclosed?',
    ],
  },
]
