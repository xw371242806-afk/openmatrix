import json
from agent.state_machine import ResearchRun, StepRecord, StepStatus

def test_summary_exposes_bounded_output_and_evidence():
    run = ResearchRun('r1', 'Find revenue', conclusion='Revenue was $1.4M')
    run.add_step(StepRecord('s1', 'retrieve_context', status=StepStatus.SUCCEEDED, output=json.dumps({'padding': 'x' * 5000, 'evidence': [{'page': 89, 'document_index': 0, 'cell_id': 'c1'}]})))
    run.add_step(StepRecord('s2', 'summarize', status=StepStatus.SUCCEEDED, output={'evidence': [{'page': 90}]}))
    run.add_step(StepRecord('s3', 'plain_text', status=StepStatus.SUCCEEDED, output='y' * 5001))
    summary = run.summary()
    assert summary['conclusion'] == 'Revenue was $1.4M'
    assert len(summary['steps'][0]['output_preview']) == 4000
    assert summary['steps'][0]['evidence'] == [{'page': 89, 'document_index': 0, 'cell_id': 'c1'}]
    assert summary['steps'][1]['output_preview'] == '{"evidence": [{"page": 90}]}'
    assert len(summary['steps'][2]['output_preview']) == 4000

def test_summary_preserves_real_retrieval_chunk_sources():
    run=ResearchRun('r2','Find revenue')
    run.add_step(StepRecord('s1','retrieve_context',status=StepStatus.SUCCEEDED, output=json.dumps({'chunks':[{'page':66,'chunk_id':'p66c1','quote':'Revenue'}]})))
    assert run.summary()['steps'][0]['evidence']==[{'page':66,'chunk_id':'p66c1','quote':'Revenue'}]
