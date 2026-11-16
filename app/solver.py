"""精确有理数线性可行性与可行区间求解 (两阶段单纯形法)。

问题: 给定 m 个混池对 n 个样本的线性方程
        sum_j A[i][j] * x_j = b[i],  x_j >= 0
求每个样本浓度 x_j 在全部可行解上的最小值与最大值。

为什么不用 Fourier-Motzkin: FM 是指数级算法, 在 8 池 8 样本满秩时
行数爆炸 (实测内存数 GB、数分钟不收敛)。这里改用精确两阶段单纯形:
  * 全部系数使用 fractions.Fraction, 结论与端点数学精确, 不引入浮点误差;
  * 第一阶段 (人工变量) 判可行性: 最优人工目标 > 0 即无解;
  * 第二阶段对每个样本分别最小化、最大化 x_j, 得到可行下界/上界;
  * 采用 Bland 反循环规则, 保证终止;
  * 规模 ≤ 8×8, 分数位数有界, 性能充裕。

唯一性判定: 若每个样本的下界==上界, 可行域只有一个点 (唯一解);
只要有一个样本下界<上界, 凸可行域中就有无穷多可行点 (非唯一)。
任意一个最优顶点只是"一个可行解", 绝不能当作还原值上报——本模块
对外只给可行区间与唯一/非唯一结论。
"""
from __future__ import annotations

from fractions import Fraction
from typing import Optional


class RationalError(ValueError):
    """浓度字符串不是合法的非负有理数。"""


# ---------------------------------------------------------------------------
# 输入解析与展示
# ---------------------------------------------------------------------------

def parse_nonneg_rational(text) -> Fraction:
    """把用户输入解析为非负有理数 Fraction。

    接受整数 "3"、小数 "0.25"、分数 "2/3"、带分数 "1_1/2" 或 "1 1/2";
    拒绝负数、非数字、零/负分母、多斜杠等非法形式。
    """
    if text is None:
        raise RationalError("浓度不能为空")
    s = str(text).strip().replace(" ", "")
    if not s:
        raise RationalError("浓度不能为空")
    try:
        if "/" in s:
            parts = s.split("/")
            if len(parts) != 2:
                raise RationalError(f"非法分数: {text!r}")
            num_s, den_s = parts
            den = int(den_s)
            if den <= 0:
                raise RationalError(f"分母必须为正: {text!r}")
            if "_" in num_s:
                whole_s, frac_num_s = num_s.split("_")
                whole = int(whole_s)
                frac_num = int(frac_num_s)
                if whole < 0 or frac_num < 0:
                    raise RationalError(f"浓度必须非负: {text!r}")
                value = Fraction(whole * den + frac_num, den)
            else:
                value = Fraction(int(num_s), den)
        elif "." in s:
            value = Fraction(s)
        elif "_" in s:
            raise RationalError(f"非法有理数: {text!r}")
        else:
            value = Fraction(int(s), 1)
    except (ValueError, ZeroDivisionError) as exc:
        raise RationalError(f"非法有理数: {text!r} ({exc})") from exc
    if value < 0:
        raise RationalError(f"浓度必须非负: {text!r}")
    return value


def format_fraction(value: Optional[Fraction]) -> str:
    """分数展示: 整数原样; 普通分数显示 'p/q'; None (无上界) 显示 +∞。"""
    if value is None:
        return "+∞"
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


# ---------------------------------------------------------------------------
# 精确单纯形表
#
# 约束形式 A x = b, x >= 0。为每行引入人工变量 a_i >= 0:
#   a_i = b_i - sum_j A[i][j] x_j
# 字典/表中 basis[i] 记录第 i 行当前基变量的全局下标;
# 变量 0..n-1 为原变量, n..n+m-1 为人工变量。
# ---------------------------------------------------------------------------

class _Simplex:
    def __init__(self, A: list[list[Fraction]], b: list[Fraction]):
        self.m = len(A)
        self.n = len(A[0])
        self.A = [[Fraction(v) for v in row] for row in A]
        self.b = [Fraction(v) for v in b]
        # 初始基 = 人工变量
        self.basis = [self.n + i for i in range(self.m)]
        # 表矩阵 tab[i][j]: 约束行 i 中变量 j 的系数; rhs[i] 为右端项
        self.tab = [row[:] + [Fraction(1) if k == i else Fraction(0)
                              for k in range(self.m)]
                    for i, row in enumerate(self.A)]
        self.rhs = self.b[:]
        self.art_cols = list(range(self.n, self.n + self.m))
        self.total_cols = self.n + self.m

    def _pivot(self, enter: int, leave_row: int) -> None:
        """对 (进入列 enter, 离开行 leave_row) 做 Gauss-Jordan 主元消去。"""
        pivot = self.tab[leave_row][enter]
        self.tab[leave_row] = [v / pivot for v in self.tab[leave_row]]
        self.rhs[leave_row] /= pivot
        for i in range(self.m):
            if i == leave_row:
                continue
            factor = self.tab[i][enter]
            if factor == 0:
                continue
            self.tab[i] = [self.tab[i][j] - factor * self.tab[leave_row][j]
                           for j in range(self.total_cols)]
            self.rhs[i] = self.rhs[i] - factor * self.rhs[leave_row]
        self.basis[leave_row] = enter

    def _bland_step(self, reduced_cost: list[Fraction]) -> Optional[int]:
        """Bland 规则选进入列: 下标最小且检验数为负的列; 无则返回 None。"""
        for j in range(self.total_cols):
            if j in self.art_cols:
                continue
            if reduced_cost[j] < 0:
                return j
        return None

    def _leave_row(self, enter: int) -> Optional[int]:
        """最小比值测试 (Bland 平局取下标最小的基变量行)。"""
        best_row: Optional[int] = None
        best_ratio: Optional[Fraction] = None
        best_basis: Optional[int] = None
        for i in range(self.m):
            a = self.tab[i][enter]
            if a <= 0:
                continue
            ratio = self.rhs[i] / a
            base = self.basis[i]
            if (best_ratio is None or ratio < best_ratio
                    or (ratio == best_ratio and base < best_basis)):
                best_ratio = ratio
                best_row = i
                best_basis = base
        return best_row

    def _objective_row(self, c: list[Fraction]) -> tuple[list[Fraction], Fraction]:
        """由当前基还原目标函数的检验数行与当前目标值。

        目标 z = c·x; 基变量检验数需消为 0:
        reduced[j] = c[j] - sum_i c[basis_i] * tab[i][j] (基列自动为0)
        """
        reduced = [Fraction(0) for _ in range(self.total_cols)]
        c_full = c + [Fraction(0) for _ in range(self.m)]
        for j in range(self.total_cols):
            reduced[j] = c_full[j] - sum(
                c_full[self.basis[i]] * self.tab[i][j] for i in range(self.m)
            )
        z = sum(c_full[self.basis[i]] * self.rhs[i] for i in range(self.m))
        return reduced, z

    def optimize(self, c: list[Fraction]) -> tuple[Fraction, list[Fraction]]:
        """在当前表上优化目标 c·x, 返回 (最优值, 原变量最优解)。"""
        c_full = c + [Fraction(0) for _ in range(self.m)]
        while True:
            reduced, _ = self._objective_row(c_full)
            enter = self._bland_step(reduced)
            if enter is None:
                break
            leave = self._leave_row(enter)
            if leave is None:
                # 无界: 本产品的可行域有界 (样本均被非负约束且实际参与方程),
                # 理论上不会发生; 安全处理为抛出由上层映射。
                raise _UnboundedError()
            self._pivot(enter, leave)
        reduced, z = self._objective_row(c_full)
        x = [Fraction(0) for _ in range(self.n)]
        for i, base in enumerate(self.basis):
            if base < self.n:
                x[base] = self.rhs[i]
        return z, x

    def phase1(self) -> bool:
        """第一阶段: 最小化 sum 人工变量。返回 True 表示存在可行解。"""
        # 初始目标行: 人工变量系数为 -1 (最小化), 需按初始基消元
        c = [Fraction(0) for _ in range(self.n)] + [Fraction(1) for _ in range(self.m)]
        z, _ = self.optimize(c)
        return z == 0


class _UnboundedError(Exception):
    pass


def solve_feasible_bounds(
    A: list[list[Fraction]], b: list[Fraction]
) -> dict:
    """求解 A·x = b, x>=0 的每变量可行上下界。

    返回:
      {"status": "infeasible"} 或
      {"status": "unique"|"non_unique",
       "bounds": [{"lower": Fraction, "upper": Fraction}, ...]}
    """
    n = len(A[0])

    # 第一阶段: 可行性
    phase1 = _Simplex(A, b)
    feasible = phase1.phase1()
    if not feasible:
        return {"status": "infeasible", "bounds": []}

    # 以第一阶段结束的表为起点, 对每个变量分别求 min / max。
    # 每次优化都从同一张可行表复制, 避免目标切换污染。
    bounds: list[dict] = []
    all_unique = True
    for target in range(n):
        # 最小化 x_target: c = 单位向量
        c_min = [Fraction(1) if j == target else Fraction(0) for j in range(n)]
        smin = _clone_simplex(phase1)
        lower, _ = smin.optimize(c_min)

        # 最大化 x_target: c = -单位向量
        c_max = [Fraction(-1) if j == target else Fraction(0) for j in range(n)]
        smax = _clone_simplex(phase1)
        neg_upper, _ = smax.optimize(c_max)
        upper = -neg_upper

        if lower != upper:
            all_unique = False
        bounds.append({"lower": lower, "upper": upper})

    return {
        "status": "unique" if all_unique else "non_unique",
        "bounds": bounds,
    }


def _clone_simplex(src: _Simplex) -> _Simplex:
    """深拷贝一张单纯形表 (含当前基与右端项), 供独立优化使用。"""
    clone = _Simplex.__new__(_Simplex)
    clone.m = src.m
    clone.n = src.n
    clone.A = [row[:] for row in src.A]
    clone.b = src.b[:]
    clone.basis = src.basis[:]
    clone.tab = [row[:] for row in src.tab]
    clone.rhs = src.rhs[:]
    clone.art_cols = src.art_cols[:]
    clone.total_cols = src.total_cols
    return clone
