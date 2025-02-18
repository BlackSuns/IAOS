# -*- coding: utf-8 -*-
__author__ = 'carl'

import warnings
from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta
from pandas import DataFrame, Series

from db.mymysql.mysql_helper import MySqLHelper
from db.myredis.redis_cli import RedisClient
from entity.singleton import Singleton
from quotation.captures.tsdata_capturer import TuShareDataCapturer
from quotation.cleaning.data_clean import BaseDataClean
from util.quant_util import get_price, get_period_fl_trade_date

warnings.filterwarnings("ignore")

"""
因子有效性校验：
目前该模块支持 BaseDataClean.get_certainday_base_stock_infos 返回字段的校验;
继承复写重写 get_factor_data 即可;
默认取上证指数 000001.SH 为对比 基准;

针对每个候选因子:
0- 获取股票池，取近7年内均有数据的股票，以此为基准
1- 选择最近7年（每年每个月月中的数据）内每个股票数据
2- 每个月中计算每只股票的 因子X ，并排序打分，分为5组
3- 计算因子X在5个分组中的 年化复合收益率、超额收益、收益与分值相关性

检验有效性的量化标准：
1- 序列1-n的组合，年化复合收益应满足一定排序关系，即组合因子大小与收益具有较大相关关系。
假定序列i的组合年化收益为Xi,则Xi与i的相关性绝对值Abs(Corr(Xi,i))>min_corr。此处min_corr为给定的最小相关阀值。

2- 序列1和n表示的两个极端组合超额收益分别为AR1、ARn。min_top、min_bottom 表示最小超额收益阀值。
if AR1 > ARn #因子越小，收益越大
则应满足AR1 > min_top >0 and ARn < min_bottom < 0
if AR1 < ARn #因子越小，收益越大
则应满足ARn > min_top >0 and AR1 < min_bottom < 0
以上条件保证因子最大和最小的两个组合，一个明显跑赢市场，一个明显跑输市场。

3- 在任何市场行情下，1和n两个极端组合，都以较高概率跑赢or跑输市场。
以上三个条件，可以选出过去一段时间有较好选股能力的因子。


评判指标：
组合累积收益
因子平均年化收益
因子年化超额收益
高收益组合跑赢概率
低收益组合跑输概率
正收益月占比
因子IC

change log：
2023.04.20:
由于多次从数据源拉取数据极不稳定，且数据量不大 且有重复数据 所以
因子基础数据放在本地内存 样本股票收盘价格放在数据库

"""


# noinspection DuplicatedCode,PyRedundantParentheses,PyListCreation,PyNoneFunctionAssignment,PyMethodMayBeStatic
class FactorValidityCheck(Singleton):

    def __init__(self, benchmark: str = "000001.SH", factors: list = None, sample_periods: int = 7):
        self.rediscli = RedisClient().get_redis_cli()
        self.db = MySqLHelper()
        self.tsdatacapture: TuShareDataCapturer = TuShareDataCapturer()
        self.benchmark = benchmark
        self.benchmark_map = {'000001.SH': '上证综指', '399001.SZ': '深证成指',
                              '000300.SH': '沪深300', '399006.SZ': '创业板指',
                              '000016.SH': '上证50', '000905.SH': '中证500',
                              '399005.SZ': '中小板指', '000010.SH': '上证180'}
        if self.benchmark not in self.benchmark_map.keys():
            self.benchmark = list(self.benchmark_map.keys())[0]
        # 基准收益[只计算一次]
        self.benchmark_port_profit = None
        self.factors = factors
        self.sample_periods = sample_periods
        if not self.factors:
            self.default_factors()
        self.sample_trade_dates = []
        # {
        #     'trade_date':DataFrame,
        #     'trade_date': DataFrame,
        #     ...
        # }
        self.factor_basics_data = None
        # factors ic
        self.factors_ics = DataFrame()
        # factor ic最小相关阀值
        self.min_corr = 0.5
        # 最小超额收益阀值
        self.min_bottom = -0.05
        # 最小超额收益阀值
        self.min_top = 0.05

        # 因子分组月收益
        self.monthly_return = DataFrame()
        # 因子总收益
        self.total_return = {}
        # 因子平均年化收益
        self.annual_return = {}
        # 超额收益
        self.excess_return = {}
        # 赢家组合跑赢概率
        self.win_prob = {}
        # 输家组合跑输概率
        self.loss_prob = {}
        # effect_test["ic"]记录因子相关性，>0.5或<-0.5合格
        # effect_test["excess"]记录 赢家组合超额收益，输家组合超额收益
        # effect_test["prob"]记录 赢家组合跑赢概率和输家组合跑输概率;【>0.5,>0.4】合格(因实际情况，跑输概率暂时不考虑)
        self.effect_test = {}
        self.effect_test_df = None
        # 符合 检验有效性的量化标准 的因子
        self.effective_factors = None

    def init_data(self, refresh):
        """
        初始化和落地所需数据
        1- factor_data放内存
        2- 样本股票收盘价格放在数据库
        """
        if refresh:
            sql = r'delete from sample_stk_price'
            self.db.delete(sql)
        self.factor_basics_data = {}
        now_m = datetime.today().month
        now_y = datetime.today().year
        now_d = datetime.today().day
        now_date = datetime(now_y, now_m, now_d)
        start_date = datetime(now_y - self.sample_periods, 1, 1)
        while start_date + relativedelta(days=+1) <= now_date:
            end_date = start_date + relativedelta(months=+1)
            if end_date >= now_date:
                end_date = now_date
            start_date_str = str(start_date.year) + str(start_date.month).zfill(2) + str(start_date.day).zfill(2)
            end_date_str = str(end_date.year) + str(end_date.month).zfill(2) + str(end_date.day).zfill(2)
            trade_start_date, trade_end_date = get_period_fl_trade_date(start_date=start_date_str,
                                                                        end_date=end_date_str)
            if trade_start_date is None:
                break
            # 初始化因子行情数据
            basics_data = self.load_factor_data(trade_start_date)
            self.factor_basics_data[trade_start_date] = basics_data
            self.sample_trade_dates.append(trade_start_date)
            if refresh:
                # 样本股票收盘价格放在数据库
                ts_codes = list(basics_data['ts_code'])
                # 'ts_code', 'close' 'trade_date' 'asset'
                close1 = get_price(ts_code_list=ts_codes, trade_date=trade_start_date)
                close2 = get_price(ts_code_list=[self.benchmark], trade_date=trade_start_date, asset='I')
                for close_data in close1, close2:
                    keys = close_data.keys()
                    values = [tuple(val) for val in close_data.values.tolist()]
                    key_sql = ','.join(keys)
                    value_sql = ','.join(['%s'] * close_data.shape[1])
                    # 插入语句
                    insert_data_str = """ insert into %s (%s) values (%s)""" % ('sample_stk_price', key_sql, value_sql)
                    self.db.insertmany(sql=insert_data_str, param=values)
            start_date = end_date

    def get_validity_all_factors(self, refresh=False):
        """
        获取 符合 检验有效性的量化标准 的因子
        """
        # 初始化和落地所需数据
        self.init_data(refresh=refresh)
        if not self.effect_test_df:
            self.check_all_factor_validity()

    def check_factor_validity(self, fac):
        """检验有效性的量化标准"""
        # 获取特定因子指定周期内月收益
        self.gather_monthly_return(fac)
        # 计算特定因子评判指标
        self.effect_test[fac] = {}
        monthly = self.monthly_return[[fac]]
        # 计算因子平均年化收益
        # to see https://zhuanlan.zhihu.com/p/390849319
        # 复利的本息计算公式是：F=P（1+i)^n P=本金，i=利率，n=期限
        # 总收益率
        fac_total_return = (monthly + 1).T.cumprod().iloc[-1, :] - 1
        self.total_return[fac] = fac_total_return
        # 各个组合平均年化收益
        # fac_annual_return = (self.total_return[fac] + 1) ** (1. / (len(monthly) / 12)) - 1
        fac_annual_return = (self.total_return[fac] + 1) ** (1. / (monthly.columns.size / 12)) - 1
        self.annual_return[fac] = fac_annual_return
        # 各个组合超额收益 【因子annual_return - 基准annual_return】
        fac_excess_return = self.annual_return[fac] - self.annual_return[fac][-1]
        self.excess_return[fac] = fac_excess_return
        # 判断因子有效性
        # 1.年化收益与因子的相关性IC
        fac_ic = self.annual_return[fac][0:5].corr(
            Series([1, 2, 3, 4, 5], index=self.annual_return[fac][0:5].index))
        self.effect_test[fac]["ic"] = fac_ic

        # 2.高收益组合跑赢概率  port_1因子<port_5因子
        # 因子小，收益小，port_1是输家组合，port_5是赢家组合
        if self.total_return[fac][0] < self.total_return[fac][-2]:
            loss_excess = monthly.iloc[0, :] - monthly.iloc[-1, :]
            self.loss_prob[fac] = loss_excess[loss_excess < 0].count() / float(len(loss_excess))
            win_excess = monthly.iloc[-2, :] - monthly.iloc[-1, :]
            self.win_prob[fac] = win_excess[win_excess > 0].count() / float(len(win_excess))
            # 赢家组合跑赢概率和输家组合跑输概率
            self.effect_test[fac]["prob"] = [self.win_prob[fac], self.loss_prob[fac]]
            # 超额收益
            self.effect_test[fac]["excess"] = [self.excess_return[fac][-2], self.excess_return[fac][0]]
            l_annual_return = fac_annual_return["port_1"]
            w_annual_return = fac_annual_return["port_5"]
            l_total_return = fac_total_return["port_1"]
            w_total_return = fac_total_return["port_5"]
        # 因子小，收益大，port_1是赢家组合，port_5是输家组合
        else:
            # port_5-benchmark
            loss_excess = monthly.iloc[-2, :] - monthly.iloc[-1, :]
            self.loss_prob[fac] = loss_excess[loss_excess < 0].count() / float(len(loss_excess))
            win_excess = monthly.iloc[0, :] - monthly.iloc[-1, :]
            self.win_prob[fac] = win_excess[win_excess > 0].count() / float(len(win_excess))
            # 赢家组合跑赢概率和输家组合跑输概率
            self.effect_test[fac]["prob"] = [self.win_prob[fac], self.loss_prob[fac]]
            # 超额收益
            self.effect_test[fac]["excess"] = [self.excess_return[fac][0], self.excess_return[fac][-2]]
            l_annual_return = fac_annual_return["port_5"]
            w_annual_return = fac_annual_return["port_1"]
            l_total_return = fac_total_return["port_5"]
            w_total_return = fac_total_return["port_1"]
        self.effect_test_df = (DataFrame(self.effect_test))
        self.effective_factors = self.effect_test_df.copy(deep=True)

        self.save_fac_valid_info(fac, fac_annual_return, fac_ic, fac_total_return, l_annual_return, l_total_return,
                                 w_annual_return, w_total_return)
        self.draw_return_picture(fac)

    def check_all_factor_validity(self):
        """检验有效性的量化标准"""
        for fac in self.factors:
            self.check_factor_validity(fac=fac)

    def gather_monthly_return(self, factor):
        """
        集合 monthly_return
        """
        flag = 0
        factor_port_profit = self.cal_factor_ports_monthly_return(factor=factor)
        if len(factor_port_profit) < 6 and self.benchmark_port_profit is not None:
            factor_port_profit['benchmark'] = self.benchmark_port_profit
        self.benchmark_port_profit = factor_port_profit['benchmark']
        fac_port_profit = DataFrame(factor_port_profit).T
        columns = pd.MultiIndex.from_product([[factor], fac_port_profit.columns])
        fac_port_profit.columns = columns
        if flag == 0:
            self.monthly_return = fac_port_profit
        else:
            self.monthly_return = self.monthly_return.join(fac_port_profit)
        flag += 1
        del flag

    def cal_factor_ports_monthly_return(self, factor="pe_ttm"):
        """
        计算某一个因子在各个分组的月收益率
        """
        port_profit = {}
        for i in range(len(self.sample_trade_dates)):
            if i + 1 >= len(self.sample_trade_dates):
                break
            start_date = self.sample_trade_dates[i]
            end_date = self.sample_trade_dates[i + 1]
            basics_data = self.get_factor_data(factor, start_date)
            self.part_data_cal_mon_profit(basics_data, end_date, factor, port_profit, start_date)
            # 计算基准月收益[只计算一次]
            if self.benchmark_port_profit is None:
                benchmark_m_return = self.cal_benchmark_monthly_return(start_date, end_date)
                if not port_profit.keys().__contains__("benchmark"):
                    prof_list = []
                    prof_list.append(benchmark_m_return)
                    port_profit["benchmark"] = prof_list
                else:
                    port_profit["benchmark"].append(benchmark_m_return)
        return port_profit

    def part_data_cal_mon_profit(self, basics_data, end_date, factor, port_profit, start_date):
        """分组 并计算 分组月收益"""
        score = basics_data[['ts_code', factor]].sort_values(by=factor)
        # 流通市值
        cmv = basics_data[['ts_code', 'CMV']]
        cmv.index = cmv['ts_code']
        port1 = list(score['ts_code'])[: len(score.index) // 5]
        port2 = list(score['ts_code'])[len(score.index) // 5: 2 * len(score.index) // 5]
        port3 = list(score['ts_code'])[2 * len(score.index) // 5: -2 * len(score.index) // 5]
        port4 = list(score['ts_code'])[-2 * len(score.index) // 5: -len(score.index) // 5]
        port5 = list(score['ts_code'])[-len(score.index) // 5:]
        ports = [port1, port2, port3, port4, port5]
        port_index = 0
        for port in ports:
            port_index += 1
            weighted_m_return = self.cal_port_monthly_return(port, start_date, end_date, cmv)
            if not port_profit.keys().__contains__("port_" + str(port_index)):
                prof_list = []
                prof_list.append(weighted_m_return)
                port_profit["port_" + str(port_index)] = prof_list
            else:
                port_profit["port_" + str(port_index)].append(weighted_m_return)

    def load_factor_data(self, trade_date):
        """
        获取含有因子数据的行情
        如果所需因子不在 get_certainday_base_stock_infos 需要重写该方法
        """
        need_cols = self.factors.copy()
        need_cols.insert(0, 'ts_code')
        basics_data: DataFrame = BaseDataClean.get_certainday_base_stock_infos(trade_date=trade_date)[need_cols]
        basics_data['CMV'] = basics_data['circ_mv']
        return basics_data

    def get_factor_data(self, factor, trade_date):
        """
        获取含有因子数据的行情
        """
        basics_data: DataFrame = self.factor_basics_data[trade_date][[
            'ts_code', 'CMV', factor]]
        basics_data.dropna(axis=0, how='any', subset=[factor], inplace=True)
        return basics_data

    # @retry(max_retry=3, time_interval=9)
    def cal_port_monthly_return(self, port, startdate, enddate, CMV):
        """
        计算分组内流通市值加权的月收益率
        """
        port_codes = []
        value_sql = ','.join(['%s'] * len(port))
        sql = r"""select ts_code,close from sample_stk_price where trade_date=%s and ts_code in ({}) and asset=%s""".format(
            value_sql)
        port_codes.append(startdate)
        for p in port:
            port_codes.append(p)
        port_codes.append('E')
        params1 = tuple(port_codes)
        port_codes[0] = enddate
        params2 = tuple(port_codes)
        data1 = self.db.selectall(sql=sql, param=params1)
        data2 = self.db.selectall(sql=sql, param=params2)

        columns = ['ts_code', 'close']
        close1 = pd.DataFrame([list(i) for i in data1], columns=columns)
        close1.index = close1['ts_code']
        close2 = pd.DataFrame([list(i) for i in data2], columns=columns)
        close2.index = close2['ts_code']
        c1_list = close1['ts_code'].to_list()
        c2_list = close2['ts_code'].to_list()
        valid_codes = list(set(c1_list).intersection(set(c2_list)))
        close1 = close1['close'].loc[valid_codes]
        close2 = close2['close'].loc[valid_codes]
        # 组合月收益率集合
        month_profit = close2 / close1 - 1
        circ_mv = CMV['CMV'].loc[valid_codes]
        # 月收益率加权流通市值
        weighted_month_profit = month_profit * circ_mv
        # 根据流通市值加权的月收益率
        weighted_m_return = weighted_month_profit.sum() / circ_mv.sum()
        return weighted_m_return

    def cal_benchmark_monthly_return(self, startdate, enddate):
        """
        计算分组内基准的月收益率
        """
        sql = r"""select ts_code,close from sample_stk_price where trade_date=%s and ts_code =%s and asset=%s"""
        params1 = (startdate, self.benchmark, 'I')
        params2 = (enddate, self.benchmark, 'I')
        data1 = self.db.selectall(sql=sql, param=params1)
        data2 = self.db.selectall(sql=sql, param=params2)
        columns = ['ts_code', 'close']
        close1 = pd.DataFrame([list(i) for i in data1], columns=columns)
        close2 = pd.DataFrame([list(i) for i in data2], columns=columns)
        close1.index = close1['ts_code']
        close2.index = close2['ts_code']
        valid_codes = close1['ts_code'].to_list()
        close1 = close1['close'].loc[valid_codes]
        close2 = close2['close'].loc[valid_codes]
        benchmark_return = (close2 / close1 - 1).sum()
        return benchmark_return

    def save_fac_valid_info(self, fac, fac_annual_return, fac_ic, fac_total_return, l_annual_return, l_total_return,
                            w_annual_return, w_total_return):
        """
        保存因子有效性信息
        """
        sql1 = r'select * from candidate_factors where factor_id=%s'
        args = fac
        res = self.db.selectone(sql=sql1, param=args)
        factor_id = res[1]
        factor_name = res[2]
        factor_type_id = res[3]
        factor_type = res[4]
        benchmark = self.benchmark
        benchmark_name = self.benchmark_map[self.benchmark]
        benchmark_total_return = fac_total_return["benchmark"]
        benchmark_annual_return = fac_annual_return["benchmark"]
        win_total_return = w_total_return
        win_annual_return = w_annual_return
        # effect_test["excess"]记录 赢家组合超额收益，输家组合超额收益
        # effect_test["prob"]记录 赢家组合跑赢概率和输家组合跑输概率;【>0.5,>0.4】合格(因实际情况，跑输概率暂时不考虑)
        win_excess_return = self.effect_test[fac]["excess"][0]
        loss_total_return = l_total_return
        loss_annual_return = l_annual_return
        loss_excess_return = self.effect_test[fac]["excess"][1]
        win_prob = self.win_prob[fac]
        loss_prob = self.loss_prob[fac]
        factor_ic = fac_ic
        # 1-有效 0-无效
        is_valid = 1
        if abs(fac_ic) < self.min_corr:
            is_valid = 0
        sample_periods = self.sample_periods
        memo = ''
        sql2 = r'select * from factor_validity_info where factor_id=%s'
        args = fac
        res = self.db.selectone(sql=sql2, param=args)
        if res is not None:
            sql3 = r'delete from factor_validity_info WHERE factor_id=%s'
            args = fac
            self.db.delete(sql3, args)
        sql4 = r"""insert into factor_validity_info
               (factor_id,factor_name,factor_type_id,factor_type,benchmark,
               benchmark_name,benchmark_total_return,benchmark_annual_return,win_total_return,
               win_annual_return,win_excess_return,loss_total_return,loss_annual_return,
               loss_excess_return,win_prob,loss_prob,factor_ic,is_valid,sample_periods,memo) 
               values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
        self.db.insertone(sql4, (factor_id, factor_name, factor_type_id, factor_type, benchmark,
                                       benchmark_name, benchmark_total_return, benchmark_annual_return,
                                       win_total_return, win_annual_return, win_excess_return, loss_total_return,
                                       loss_annual_return, loss_excess_return, win_prob, loss_prob, factor_ic,
                                       is_valid, sample_periods, memo))

    def default_factors(self):
        """默认因子集"""
        # ['pe', 'pe_ttm', 'pb', 'ps', 'ps_ttm', 'dv_ratio', 'eps', 'bps', 'roe', 'roe_yearly', 'npta', 'roa',
        #  'roa_yearly', 'roa2_yearly', 'roic', 'basic_eps_yoy', 'op_yoy', 'ebt_yoy', 'tr_yoy', 'or_yoy', 'equity_yoy',
        #  'netprofit_margin', 'grossprofit_margin', 'profit_to_gr', 'op_of_gr', 'debt_to_assets', 'total_mv', 'circ_mv',
        #  'volume', 'amount', 'current_ratio', 'quick_ratio', 'turnover_rate', 'turnover_rate_f', 'volume_ratio',
        #  'changepercent']
        # ['市盈率', '市盈率TTM', '市净率', '市销率', '市销率TTM', '股息率', '基本每股收益', '每股净资产', '净资产收益率', '年化净资产收益率', '总资产净利润', '总资产报酬率',
        #  '年化总资产净利率', '年化总资产报酬率', '投入资本回报率', '基本每股收益(eps)同比增长率', '营业利润同比增长率', '利润总额同比增长率', '营业总收入同比增长率', '营业收入同比增长率',
        #  '净资产同比增长率', '销售净利率', '销售毛利率', '净利润率', '营业利润率', '资产负债率', '总市值', '流通市值', '成交量', '成交额', '流动比率', '速动比率', '换手率',
        #  '换手率（自由流通股）', '量比', '涨跌幅']
        sql = r'select * from candidate_factors'
        res = self.db.selectall(sql=sql)
        self.factors = [item[1] for item in res]

    def draw_return_picture(self, fac):
        df = self.monthly_return[[fac]]
        plt.rcParams['font.sans-serif'] = ['Microsoft YaHei']  # 用来正常显示中文标签
        plt.rcParams['axes.unicode_minus'] = False  # 用来正常显示负号
        plt.xticks(size=12, rotation=50)  # 设置字体大小和字体倾斜度
        fig = plt.figure()
        fig.suptitle('Figure: return for %s' % fac)
        # (df.T + 1).cumprod().iloc[:, 0].plot(label='port1')
        plt.plot(np.array((df.T + 1).cumprod().iloc[:, 0]), label='port1')
        plt.plot(np.array((df.T + 1).cumprod().iloc[:, 1]), label='port2')
        # (df.T + 1).cumprod().iloc[:, 1].plot(label='port2')
        # (df.T + 1).cumprod().iloc[:, 2].plot(label='port3')
        plt.plot(np.array((df.T + 1).cumprod().iloc[:, 2]), label='port3')
        plt.plot(np.array((df.T + 1).cumprod().iloc[:, 3]), label='port4')
        # (df.T + 1).cumprod().iloc[:, 3].plot(label='port4')
        # (df.T + 1).cumprod().iloc[:, 4].plot(label='port5')
        # (df.T + 1).cumprod().iloc[:, 5].plot(label='benchmark')
        plt.plot(np.array((df.T + 1).cumprod().iloc[:, 4]), label='port5')
        plt.plot(np.array((df.T + 1).cumprod().iloc[:, 5]), label='benchmark')
        plt.xlabel('return of factor %s' % fac)
        plt.legend(loc=0)
        plt.show()
