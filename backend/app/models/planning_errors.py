"""跨规划模块共享的安全错误类型。"""
class PlanningError(RuntimeError):
    def __init__(self, code, message, step=None):
        super().__init__(message)
        self.code, self.step = code, step



class InvalidPlanningReferences(Exception):
    def __init__(self, codes, *, activity_issue_codes=None):
        self.codes = tuple(sorted(set(codes)))
        self.activity_issue_codes = activity_issue_codes or {}
