################## Imports ##################
# Remember to properly add the packages to requirements.txt or conda-requirements.txt. 
from __future__ import division

from flask import Flask, render_template, request, redirect, send_file
import requests
import os
import sys

from bokeh.plotting import figure
from bokeh.embed import components
from bokeh.layouts import row
from bokeh.models import Legend

import pandas_datareader.data as web
import scipy.stats as stat
import pandas as pd
import numpy as np
import datetime
import dateutil.relativedelta


##################  Function definitions ##################

# Create dataframe from input for individual stock
def create_df_from_tickers(tickers_string, position_date, end_date):
    tickers_list = tickers_string.replace(" ", "").split(",")
    start_date = position_date - dateutil.relativedelta.relativedelta(years = 10)
    d={}
    for ticker in tickers_list:
        d["{0}".format(ticker)] = web.DataReader(ticker, 'yahoo', start_date, end_date)['Adj Close'].rename(ticker)
    df = pd.DataFrame(d).sort_index(ascending = False)
    plot_length = len(df[df.index >= position_date])
    return df, plot_length

# Create dataframe from input for portfolio
def create_df_from_tickers_port(tickers_string, weights_string, v0, position_date, end_date):
    tickers_list = tickers_string.replace(" ", "").split(",")
    weights_list = map(float, weights_string.split(","))
    start_date = position_date - dateutil.relativedelta.relativedelta(years = 10)
    d={}
    for ticker in tickers_list:
        d["{0}".format(ticker)] = web.DataReader(ticker, 'yahoo', start_date, end_date)['Adj Close'].rename(ticker)
    df = pd.DataFrame(d).sort_index(ascending = False)
    plot_length = len(df[df.index >= position_date])
    shares = np.round(np.divide(v0 * np.array(weights_list), np.array(df.ix[position_date])))
    portfolio = pd.DataFrame({'Portfolio_%s' % (tickers_string.replace(",", "_")): np.matmul(df, shares)}, index = df.index)
    return portfolio, plot_length

# Price plot
def plot_price(price, length):
    data = price[:length]
    output_file = 'outputs/price_%s_%s_%s.csv' % (data.columns.values[0], data.index[-1].date(), data.index[0].date())
    data.to_csv(output_file)
    plot = figure(width=600, height=400, title = "%s Historical Prices" % data.columns.values[0], 
                  x_axis_label='Date', y_axis_label='Price', x_axis_type="datetime")
    plot.line(data.index, data)
    plot.title.text_font_size = '12pt'
    return plot, output_file

# Calculate estimated parameters for GBM based on x year (in days) rolling windows
def gbm_est(prices, window_days):
    rtn = -np.diff(np.log(prices))
    rtnsq = rtn * rtn
    mubar = list(reversed(np.convolve(rtn, np.ones((window_days,))/window_days, mode='valid')))
    x2bar = list(reversed(np.convolve(rtnsq, np.ones((window_days,))/window_days, mode='valid')))
    var = x2bar - np.square(mubar)
    sigmabar = np.sqrt(np.maximum(var, np.zeros(len(var))))
    sigma = sigmabar / np.sqrt(1/252)
    mu = np.array(mubar)*252 + np.square(sigma)/2
    return rtn, mu, sigma, np.array(mubar), sigmabar

# Parameter plot
def plot_parameters(price):
    rtn_2, mu_2, sigma_2, mubar_2, sigmabar_2 = gbm_est(price, 2*252)
    rtn_5, mu_5, sigma_5, mubar_5, sigmabar_5 = gbm_est(price, 5*252)
    rtn_10, mu_10, sigma_10, mubar_10, sigmabar_10 = gbm_est(price, 10*252)
    length = min(len(mu_2), len(mu_5), len(mu_10), len(sigma_2), len(sigma_5), len(sigma_10))
    mu = pd.DataFrame({'Mu_2': mu_2[:length], 'Mu_5': mu_5[:length], 'Mu_10': mu_10[:length]}, 
                      index = price.index[:length])
    sigma = pd.DataFrame({'Sigma_2': sigma_2[:length], 'Sigma_5': sigma_5[:length], 'Sigma_10': sigma_10[:length]}, 
                         index = price.index[:length])
    output_file = 'outputs/mu_sigma_%s_%s_%s.csv' % (price.name, mu.index[-1].date(), mu.index[0].date())
    pd.merge(mu, sigma, left_index=True, right_index=True).to_csv(output_file)
    pmu = figure(width=600, height=400, title = "%s Mu" % price.name, 
                 x_axis_label='Date', y_axis_label='Mu', x_axis_type="datetime")
    pmu.line(mu.index, mu['Mu_2'], legend = '2-year rolling window')
    pmu.line(mu.index, mu['Mu_5'], color = 'green', legend = '5-year rolling window')
    pmu.line(mu.index, mu['Mu_10'], color = 'orange', legend = '10-year rolling window')
    pmu.title.text_font_size = '12pt'
    pmu.legend.location = 'top_left'
    pmu.legend.background_fill_alpha = 0.5
    psigma = figure(width=600, height=400, title = "%s Sigma" % price.name, 
                    x_axis_label='Date', y_axis_label='Sigma', x_axis_type="datetime")
    psigma.line(mu.index, sigma['Sigma_2'], legend = '2-year rolling window')
    psigma.line(mu.index, sigma['Sigma_5'], color = 'green', legend = '5-year rolling window')
    psigma.line(mu.index, sigma['Sigma_10'], color = 'orange', legend = '10-year rolling window')
    psigma.title.text_font_size = '12pt'
    psigma.legend.location = 'top_left'
    psigma.legend.background_fill_alpha = 0.5
    plot = row(pmu, psigma)
    return plot, output_file

# Calculate VaR and ES using parametric method
def parametric(v0, mu, sigma, VaR_prob, ES_prob, t):
    VaR = v0 - v0 * np.exp(sigma * np.sqrt(t) * stat.norm.ppf(1-VaR_prob) + (mu - np.square(sigma)/2) * t)
    ES = v0 * (1 - np.array(stat.norm.cdf(stat.norm.ppf(1-ES_prob) - np.sqrt(t)*sigma)) * np.array(np.exp(mu*t)/(1-ES_prob)))
    return VaR, ES

# Calculate VaR and ES using historical method
def historical(v0, price, VaR_prob, ES_prob, window_days, horizon_days):
    npaths = window_days - horizon_days
    ntrials = len(price) - window_days
    price_log = np.log(price)
    return_xdays = np.array(price_log[:(len(price_log)-horizon_days)]) - np.array(price_log[5:])
    price_res = v0 * np.exp(return_xdays)
    scenarios = np.zeros(shape=(npaths,ntrials))
    for i in range(ntrials):
        scenarios[0:npaths,i] = price_res[i:i+npaths]
    scenarios_sorted = np.sort(scenarios, axis=0)
    VaR = v0 - scenarios_sorted[np.ceil((1-VaR_prob)*npaths).astype(int) - 1]
    ES = v0 - np.mean(scenarios_sorted[0:(np.ceil((1-ES_prob)*npaths).astype(int))], axis=0)
    return VaR, ES

# Calculate VaR and ES using Monte Carlo method
def monte_carlo(v0, price, mu, sigma, VaR_prob, ES_prob, window_days, horizon):
    npaths = 5000
    ntrials = len(price) - window_days
    p1 = np.zeros(shape=(npaths,ntrials))
    for i in range(ntrials):
        tv = np.ones(shape =(npaths,1))*horizon
        bm = np.sqrt(horizon) * np.random.randn(npaths,1)
        y = v0 * np.exp(sigma[i] * bm - (mu[i] + sigma[i]*sigma[i]/2) * tv)
        p1[:,i] = y[:,0]
    p2 = np.sort(p1,axis = 0)
    VaR = v0 - p2[np.ceil((1-VaR_prob)*npaths).astype(int) - 1]
    ES = v0 - np.mean(p2[0:(np.ceil((1-ES_prob)*npaths).astype(int))], axis=0)
    return VaR, ES 

# VaR/ES plot
def plot_risk(v0, price, VaR_prob, ES_prob, method, window, horizon, plot_length):
    if method == 'Parametric VaR/ES':
        rtn, mu, sigma, mubar, sigmabar = gbm_est(price, window*252)
        VaR, ES = parametric(v0, mu, sigma, VaR_prob, ES_prob, horizon)
    elif method == 'Historical VaR/ES':
        VaR, ES = historical(v0, price, VaR_prob, ES_prob, int(window*252), int(horizon*252))
    elif method == 'Monte Carlo VaR/ES':
        rtn, mu, sigma, mubar, sigmabar = gbm_est(price, window*252)
        VaR, ES = monte_carlo(v0, price, mu, sigma, VaR_prob, ES_prob, window*252, horizon)
    else:
        sys.exit('Error!')        
    length = min(len(VaR), len(ES), plot_length)
    VaR_ES = pd.DataFrame({'VaR': VaR[:length], 'ES': ES[:length]}, index = price.index[:plot_length])
    output_file = 'outputs/%s_%s_%s_%s.csv' % (method.replace(" VaR/ES", "").replace(" ", "_"), price.name, 
                                               VaR_ES.index[-1].date(), VaR_ES.index[0].date())
    VaR_ES.to_csv(output_file)
    plot = figure(width=600, height=400,
                  title = "%s_%s VaR/ES" % (method.replace(" VaR/ES", "").replace(" ", "_"), price.name),
                  x_axis_type="datetime")
    plot.line(VaR_ES.index, VaR_ES['VaR'], color = 'orange', legend = 'VaR')
    plot.line(VaR_ES.index, VaR_ES['ES'], color = 'green', legend = 'ES')
    plot.legend.location = 'top_left'
    plot.legend.background_fill_alpha = 0.5
    plot.title.text_font_size = '12pt'    
    share_change = np.divide(price[:(len(price)-int(horizon*252))], price[int(horizon*252):])
    loss = v0 - share_change * v0
    length_loss = min(len(loss), len(VaR), plot_length)
    test = pd.DataFrame({'VaR': VaR[int(horizon*252):length_loss], 'Loss': loss[:(length_loss-int(horizon*252))]},
                   index = price.index[int(horizon*252):length_loss])  
    plot_test = figure(width=600, height=400,
                       title = "%s_%s VaR/ActualLoss" % (method.replace(" VaR/ES", "").replace(" ", "_"), price.name),
                       x_axis_type="datetime")
    plot_test.line(test.index, test['VaR'], color = 'orange', legend = 'VaR')
    plot_test.line(test.index, test['Loss'], color = 'green', legend = 'Actual Loss')
    plot_test.legend.location = 'top_left'
    plot_test.legend.background_fill_alpha = 0.5
    plot_test.title.text_font_size = '12pt'
    plots = row(plot,plot_test)
    return plots, output_file

# Black Scholes method to calculate put option price
def bs_put(stock, rf, sigma, strike, maturity):
    sigrt = 1/(sigma*np.sqrt(maturity))
    sig2 = sigma*sigma/2
    lsk = np.log(stock/strike)
    ert = np.exp(-rf*maturity)
    d1 = sigrt*(lsk+(rf+sig2)*maturity)
    d2 = sigrt*(lsk+(rf-sig2)*maturity)
    pr = stat.norm.cdf(-d2)*strike*ert-stat.norm.cdf(-d1)*stock
    return pr

# MC method to calculate option portfolio VaR
# Compute MC VaR for portfolio of a stock and a put option. % the stocks, assuming option implied vols are unchanged.
def option_mc(s0, mu, sigma, rf, iv, strike, mat, nstocks, nputs, VaR_prob, horizon):
    npaths = 1000000
    tv = np.ones(shape =(npaths,1))*horizon
    bm = np.sqrt(horizon) * np.random.randn(npaths,1)
    st = s0 * np.exp(sigma * bm - (mu + sigma*sigma/2) * tv)
    vtStock = st * nstocks
    v0Stock = s0 * nstocks
    putt = bs_put(st, rf, iv, strike, mat-horizon)
    vtPut = nputs * putt
    put0 = bs_put(s0, rf, iv, strike, mat)
    v0Put = nputs * put0
    loss = v0Stock + v0Put - (vtStock + vtPut)
    VaR = np.percentile(loss, 100*VaR_prob)
    return VaR

# Option portfolio calculations
def options_cal(options, rf, mat, imp_vol, v0, liq_rate, VaR_prob, window, horizon):
    rtn, mu, sigma, mubar, sigmabar = gbm_est(options, window*252)
    mu = mu[0]
    sigma = sigma[0]
    VaR_1, ES_1 = parametric(v0, mu, sigma, VaR_prob, 0.975, horizon)
    s0 = options[0]
    strike = options[0]
    nstocks = v0 * (1-liq_rate) / s0
    put0 = bs_put(s0, rf, imp_vol, strike, mat)
    nputs = v0 * liq_rate / put0
    VaR_2 = option_mc(s0, mu, sigma, rf, imp_vol, strike, mat, nstocks, nputs, VaR_prob, horizon)
    reduction = 100*(1-VaR_2/VaR_1)
    print_list = ["Stock price: %s" % s0,
                  "Stock shares: %s" % nstocks,
                  "Put price on one share: %s" % put0,
                  "Put shares: %s" % nputs,
                  "VaR without options: %s" % VaR_1,
                  "VaR with options: %s" % VaR_2,
                  "VaR reduction (percentage): %s" % reduction]
    output_file = 'outputs/options_%s_%s.csv' % (options.name, options.index[0].date())
    pd.DataFrame(print_list).to_csv(output_file, header=False)
    return s0, nstocks, put0, nputs, VaR_1, VaR_2, reduction, output_file

################## Flask & html interaction ##################

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def main():
    return redirect('/index')

@app.route('/index', methods=['GET', 'POST'])
def index():
    if request.method == 'GET':
        return render_template('index.html', opt_table_style = 'display:none',
                               tickers_string_1_value = 'AAPL', position_date_1_value = '2000-12-01',
                               end_date_1_value = '2016-12-01', v0_1_value = '10000', var_prob_1_value = '0.99',
                               es_prob_1_value = '0.975', window_year_1_value = '2', horizon_day_1_value = '5',
                               output_file_1 = 'outputs/price_AAPL_2000-12-01_2016-12-01.csv',
                               tickers_string_2_value = 'AAPL,MSFT', position_date_2_value = '2000-12-01',
                               weights_string_2_value = '0.5,0.5', end_date_2_value = '2016-12-01',
                               v0_2_value = '10000', var_prob_2_value = '0.99', es_prob_2_value = '0.975',
                               window_year_2_value = '2', horizon_day_2_value = '5',
                               output_file_2 = 'outputs/price_Portfolio_AAPL_MSFT_2000-12-01_2016-12-01.csv',
                               tickers_string_3_value = 'AAPL', position_date_3_value = '2016-12-21',
                               window_year_3_value = '2', horizon_day_3_value = '5',
                               rf_3_value = '0.005', var_prob_3_value = '0.99', mat_3_value = '0.5',
                               v0_3_value = '1000000', liq_rate_3_value = '0.01', implied_vol_3_value = '0.21',
                               output_file_3 = 'outputs/options_AAPL_2016-12-21.csv')
    else:
        # Feature 1 - Individual stock
        if 'btn_1' in request.form:
            if request.form['btn_1'] == 'Price Plot':
                tickers_string_1 = request.form["tickers_string_1"]
                position_date_1 = request.form["position_date_1"]
                position_date_1_dt = datetime.datetime.strptime(position_date_1, '%Y-%m-%d')
                end_date_1 = request.form["end_date_1"]
                end_date_1_dt = datetime.datetime.strptime(end_date_1, '%Y-%m-%d')
                df_1, plot_length_1 = create_df_from_tickers(tickers_string_1, position_date_1_dt, end_date_1_dt)
                plot_1_1, output_file_1 = plot_price(df_1, plot_length_1)
                plots = {'div_1_1': plot_1_1}
                script, div = components(plots)
                return render_template('index.html', scroll='feature1', opt_table_style = 'display:none',
                                       script = script, div_1_1 = div['div_1_1'], output_file_1 = output_file_1,
                                       tickers_string_1_value = tickers_string_1, position_date_1_value = position_date_1,
                                       end_date_1_value = end_date_1, v0_1_value = '10000', var_prob_1_value = '0.99',
                                       es_prob_1_value = '0.975', window_year_1_value = '2', horizon_day_1_value = '5',
                                       tickers_string_2_value = 'AAPL,MSFT', position_date_2_value = '2000-12-01',
                                       weights_string_2_value = '0.5,0.5', end_date_2_value = '2016-12-01',
                                       v0_2_value = '10000', var_prob_2_value = '0.99', es_prob_2_value = '0.975',
                                       window_year_2_value = '2', horizon_day_2_value = '5',
                                       output_file_2 = 'outputs/price_Portfolio_AAPL_MSFT_2000-12-01_2016-12-01.csv',
                                       tickers_string_3_value = 'AAPL', position_date_3_value = '2016-12-21',
                                       window_year_3_value = '2', horizon_day_3_value = '5',
                                       rf_3_value = '0.005', var_prob_3_value = '0.99', mat_3_value = '0.5',
                                       v0_3_value = '1000000', liq_rate_3_value = '0.01', implied_vol_3_value = '0.21',
                                       output_file_3 = 'outputs/options_AAPL_2016-12-21.csv')
            elif request.form['btn_1'] == 'Parameter Plot':
                tickers_string_1 = request.form["tickers_string_1"]
                position_date_1 = request.form["position_date_1"]
                position_date_1_dt = datetime.datetime.strptime(position_date_1, '%Y-%m-%d')
                end_date_1 = request.form["end_date_1"]
                end_date_1_dt = datetime.datetime.strptime(end_date_1, '%Y-%m-%d')
                df_1, plot_length_1 = create_df_from_tickers(tickers_string_1, position_date_1_dt, end_date_1_dt)            
                plot_1_2, output_file_1 = plot_parameters(df_1.iloc[:,0])
                plots = {'div_1_2': plot_1_2}
                script, div = components(plots)
                return render_template('index.html', scroll='feature1', opt_table_style = 'display:none',
                                       script = script, div_1_2 = div['div_1_2'], output_file_1 = output_file_1,
                                       tickers_string_1_value = tickers_string_1, position_date_1_value = position_date_1,
                                       end_date_1_value = end_date_1, v0_1_value = '10000', var_prob_1_value = '0.99',
                                       es_prob_1_value = '0.975', window_year_1_value = '2', horizon_day_1_value = '5',
                                       tickers_string_2_value = 'AAPL,MSFT', position_date_2_value = '2000-12-01',
                                       weights_string_2_value = '0.5,0.5', end_date_2_value = '2016-12-01',
                                       v0_2_value = '10000', var_prob_2_value = '0.99', es_prob_2_value = '0.975',
                                       window_year_2_value = '2', horizon_day_2_value = '5',
                                       output_file_2 = 'outputs/price_Portfolio_AAPL_MSFT_2000-12-01_2016-12-01.csv',
                                       tickers_string_3_value = 'AAPL', position_date_3_value = '2016-12-21',
                                       window_year_3_value = '2', horizon_day_3_value = '5',
                                       rf_3_value = '0.005', var_prob_3_value = '0.99', mat_3_value = '0.5',
                                       v0_3_value = '1000000', liq_rate_3_value = '0.01', implied_vol_3_value = '0.21',
                                       output_file_3 = 'outputs/options_AAPL_2016-12-21.csv')
            elif request.form['btn_1'] == 'Risk Plot':
                tickers_string_1 = request.form["tickers_string_1"]
                position_date_1 = request.form["position_date_1"]
                position_date_1_dt = datetime.datetime.strptime(position_date_1, '%Y-%m-%d')
                end_date_1 = request.form["end_date_1"]
                end_date_1_dt = datetime.datetime.strptime(end_date_1, '%Y-%m-%d')            
                v0_1 = request.form["v0_1"]
                var_prob_1 = request.form["var_prob_1"]
                es_prob_1 = request.form["es_prob_1"]
                window_year_1 = request.form["window_year_1"]
                horizon_day_1 = request.form["horizon_day_1"]
                horizon_year_1 = float(horizon_day_1)/252
                var_es_method_1 = request.form["var_es_method_1"]
                df_1, plot_length_1 = create_df_from_tickers(tickers_string_1, position_date_1_dt, end_date_1_dt)            
                plot_1_2, output_file_1 = plot_risk(int(v0_1), df_1.iloc[:,0], float(var_prob_1), float(es_prob_1), 
                                                    var_es_method_1, int(window_year_1), horizon_year_1, plot_length_1)
                plots = {'div_1_2': plot_1_2}
                script, div = components(plots)
                return render_template('index.html', scroll='feature1', opt_table_style = 'display:none',
                                       script = script, div_1_2 = div['div_1_2'], output_file_1 = output_file_1,
                                       tickers_string_1_value = tickers_string_1, position_date_1_value = position_date_1,
                                       end_date_1_value = end_date_1, v0_1_value = v0_1, var_prob_1_value = var_prob_1,
                                       es_prob_1_value = es_prob_1, window_year_1_value = window_year_1, 
                                       horizon_day_1_value = horizon_day_1,
                                       tickers_string_2_value = 'AAPL,MSFT', position_date_2_value = '2000-12-01',
                                       weights_string_2_value = '0.5,0.5', end_date_2_value = '2016-12-01',
                                       v0_2_value = '10000', var_prob_2_value = '0.99', es_prob_2_value = '0.975',
                                       window_year_2_value = '2', horizon_day_2_value = '5',
                                       output_file_2 = 'outputs/price_Portfolio_AAPL_MSFT_2000-12-01_2016-12-01.csv',
                                       tickers_string_3_value = 'AAPL', position_date_3_value = '2016-12-21',
                                       window_year_3_value = '2', horizon_day_3_value = '5',
                                       rf_3_value = '0.005', var_prob_3_value = '0.99', mat_3_value = '0.5',
                                       v0_3_value = '1000000', liq_rate_3_value = '0.01', implied_vol_3_value = '0.21',
                                       output_file_3 = 'outputs/options_AAPL_2016-12-21.csv')
            elif request.form['btn_1'] == 'Download Result Data':
                output_file_1 = request.form["output_file_1"]
                return send_file(output_file_1, mimetype='text/csv', 
                                 attachment_filename=output_file_1.split('/')[1], as_attachment=True)
            else:
                print '1'
        # Feature 2 - Portfolio
        elif 'btn_2' in request.form:
            if request.form['btn_2'] == 'Price Plot':
                tickers_string_2 = request.form["tickers_string_2"]
                weights_string_2 = request.form["weights_string_2"]
                position_date_2 = request.form["position_date_2"]
                position_date_2_dt = datetime.datetime.strptime(position_date_2, '%Y-%m-%d')
                end_date_2 = request.form["end_date_2"]
                end_date_2_dt = datetime.datetime.strptime(end_date_2, '%Y-%m-%d')
                v0_2 = request.form["v0_2"]
                df_2, plot_length_2 = create_df_from_tickers_port(tickers_string_2, weights_string_2, int(v0_2),
                                                                  position_date_2_dt, end_date_2_dt)
                plot_2_1, output_file_2 = plot_price(df_2, plot_length_2) 
                plots = {'div_2_1': plot_2_1}
                script, div = components(plots)
                return render_template('index.html', scroll='feature2', opt_table_style = 'display:none',
                                       script = script, div_2_1 = div['div_2_1'], output_file_2 = output_file_2,
                                       tickers_string_1_value = 'AAPL', position_date_1_value = '2000-12-01',
                                       end_date_1_value = '2016-12-01', v0_1_value = '10000', var_prob_1_value = '0.99',
                                       es_prob_1_value = '0.975', window_year_1_value = '2', horizon_day_1_value = '5',
                                       output_file_1 = 'outputs/price_AAPL_2000-12-01_2016-12-01.csv',
                                       tickers_string_2_value = tickers_string_2, position_date_2_value = position_date_2,
                                       weights_string_2_value = weights_string_2, end_date_2_value = end_date_2,
                                       v0_2_value = v0_2, var_prob_2_value = '0.99', es_prob_2_value = '0.975',
                                       window_year_2_value = '2', horizon_day_2_value = '5',
                                       tickers_string_3_value = 'AAPL', position_date_3_value = '2016-12-21',
                                       window_year_3_value = '2', horizon_day_3_value = '5',
                                       rf_3_value = '0.005', var_prob_3_value = '0.99', mat_3_value = '0.5',
                                       v0_3_value = '1000000', liq_rate_3_value = '0.01', implied_vol_3_value = '0.21',
                                       output_file_3 = 'outputs/options_AAPL_2016-12-21.csv')
            elif request.form['btn_2'] == 'Parameter Plot':
                tickers_string_2 = request.form["tickers_string_2"]
                weights_string_2 = request.form["weights_string_2"]
                position_date_2 = request.form["position_date_2"]
                position_date_2_dt = datetime.datetime.strptime(position_date_2, '%Y-%m-%d')
                end_date_2 = request.form["end_date_2"]
                end_date_2_dt = datetime.datetime.strptime(end_date_2, '%Y-%m-%d')
                v0_2 = request.form["v0_2"]
                df_2, plot_length_2 = create_df_from_tickers_port(tickers_string_2, weights_string_2, int(v0_2),
                                                                  position_date_2_dt, end_date_2_dt)            
                plot_2_2, output_file_2 = plot_parameters(df_2.iloc[:,0])
                plots = {'div_2_2': plot_2_2}
                script, div = components(plots)
                return render_template('index.html', scroll='feature2', opt_table_style = 'display:none',
                                       script = script, div_2_2 = div['div_2_2'], output_file_2 = output_file_2,
                                       tickers_string_1_value = 'AAPL', position_date_1_value = '2000-12-01',
                                       end_date_1_value = '2016-12-01', v0_1_value = '10000', var_prob_1_value = '0.99',
                                       es_prob_1_value = '0.975', window_year_1_value = '2', horizon_day_1_value = '5',
                                       output_file_1 = 'outputs/price_AAPL_2000-12-01_2016-12-01.csv',
                                       tickers_string_2_value = tickers_string_2, position_date_2_value = position_date_2,
                                       weights_string_2_value = weights_string_2, end_date_2_value = end_date_2,
                                       v0_2_value = v0_2, var_prob_2_value = '0.99', es_prob_2_value = '0.975',
                                       window_year_2_value = '2', horizon_day_2_value = '5',
                                       tickers_string_3_value = 'AAPL', position_date_3_value = '2016-12-21',
                                       window_year_3_value = '2', horizon_day_3_value = '5',
                                       rf_3_value = '0.005', var_prob_3_value = '0.99', mat_3_value = '0.5',
                                       v0_3_value = '1000000', liq_rate_3_value = '0.01', implied_vol_3_value = '0.21',
                                       output_file_3 = 'outputs/options_AAPL_2016-12-21.csv')
            elif request.form['btn_2'] == 'Risk Plot':
                tickers_string_2 = request.form["tickers_string_2"]
                weights_string_2 = request.form["weights_string_2"]
                position_date_2 = request.form["position_date_2"]
                position_date_2_dt = datetime.datetime.strptime(position_date_2, '%Y-%m-%d')
                end_date_2 = request.form["end_date_2"]
                end_date_2_dt = datetime.datetime.strptime(end_date_2, '%Y-%m-%d')
                v0_2 = request.form["v0_2"]
                var_prob_2 = request.form["var_prob_2"]
                es_prob_2 = request.form["es_prob_2"]
                window_year_2 = request.form["window_year_2"]
                horizon_day_2 = request.form["horizon_day_2"]
                horizon_year_2 = float(horizon_day_2)/252
                var_es_method_2 = request.form["var_es_method_2"]
                df_2, plot_length_2 = create_df_from_tickers_port(tickers_string_2, weights_string_2, int(v0_2),
                                                                  position_date_2_dt, end_date_2_dt)            
                plot_2_2, output_file_2 = plot_risk(int(v0_2), df_2.iloc[:,0], float(var_prob_2), float(es_prob_2), 
                                                    var_es_method_2, int(window_year_2), horizon_year_2, plot_length_2)
                plots = {'div_2_2': plot_2_2}
                script, div = components(plots)
                return render_template('index.html', scroll='feature2', opt_table_style = 'display:none',
                                       script = script, div_2_2 = div['div_2_2'], output_file_2 = output_file_2,
                                       tickers_string_1_value = 'AAPL', position_date_1_value = '2000-12-01',
                                       end_date_1_value = '2016-12-01', v0_1_value = '10000', var_prob_1_value = '0.99',
                                       es_prob_1_value = '0.975', window_year_1_value = '2', horizon_day_1_value = '5',
                                       output_file_1 = 'outputs/price_AAPL_2000-12-01_2016-12-01.csv',
                                       tickers_string_2_value = tickers_string_2, position_date_2_value = position_date_2,
                                       weights_string_2_value = weights_string_2, end_date_2_value = end_date_2,
                                       v0_2_value = v0_2, var_prob_2_value = var_prob_2, es_prob_2_value = es_prob_2,
                                       window_year_2_value = window_year_2, horizon_day_2_value = horizon_day_2,
                                       tickers_string_3_value = 'AAPL', position_date_3_value = '2016-12-21',
                                       window_year_3_value = '2', horizon_day_3_value = '5',
                                       rf_3_value = '0.005', var_prob_3_value = '0.99', mat_3_value = '0.5',
                                       v0_3_value = '1000000', liq_rate_3_value = '0.01', implied_vol_3_value = '0.21',
                                       output_file_3 = 'outputs/options_AAPL_2016-12-21.csv')
            elif request.form['btn_2'] == 'Download Result Data':
                output_file_2 = request.form["output_file_2"]
                return send_file(output_file_2, mimetype='text/csv', 
                                 attachment_filename=output_file_2.split('/')[1], as_attachment=True)
            else:
                print '2'
        # Feature 3 - Options
        elif 'btn_3' in request.form:
            if request.form['btn_3'] == 'Calculate':
                tickers_string_3 = request.form["tickers_string_3"]
                position_date_3 = request.form["position_date_3"]
                position_date_3_dt = datetime.datetime.strptime(position_date_3, '%Y-%m-%d')
                window_year_3 = request.form["window_year_3"]
                horizon_day_3 = request.form["horizon_day_3"]
                rf_3 = request.form["rf_3"]
                var_prob_3 = request.form["var_prob_3"]
                mat_3 = request.form["mat_3"]
                v0_3 = request.form["v0_3"]
                liq_rate_3 = request.form["liq_rate_3"]
                implied_vol_3 = request.form["implied_vol_3"]
                horizon_year_3 = float(horizon_day_3)/252
                start_date_opt = position_date_3_dt - dateutil.relativedelta.relativedelta(years = 10)
                options = web.DataReader(tickers_string_3, 'yahoo', start_date_opt, position_date_3_dt)['Adj Close'].sort_index(ascending = False).rename(tickers_string_3)
                s0, nstocks, put0, nputs, VaR_1, VaR_2, reduction, output_file_3 = options_cal(options, float(rf_3), float(mat_3), float(implied_vol_3), int(v0_3), float(liq_rate_3), float(var_prob_3), int(window_year_3), horizon_year_3)
                return render_template('index.html', scroll='feature3', opt_table_style = 'display:block',
                                       tickers_string_1_value = 'AAPL', position_date_1_value = '2000-12-01',
                                       end_date_1_value = '2016-12-01', v0_1_value = '10000', var_prob_1_value = '0.99',
                                       es_prob_1_value = '0.975', window_year_1_value = '2', horizon_day_1_value = '5',
                                       output_file_1 = 'outputs/price_AAPL_2000-12-01_2016-12-01.csv',
                                       tickers_string_2_value = 'AAPL,MSFT', position_date_2_value = '2000-12-01',
                                       weights_string_2_value = '0.5,0.5', end_date_2_value = '2016-12-01',
                                       v0_2_value = '10000', var_prob_2_value = '0.99', es_prob_2_value = '0.975',
                                       window_year_2_value = '2', horizon_day_2_value = '5',
                                       output_file_2 = 'outputs/price_Portfolio_AAPL_MSFT_2000-12-01_2016-12-01.csv',
                                       tickers_string_3_value = tickers_string_3, position_date_3_value = position_date_3,
                                       window_year_3_value = window_year_3, horizon_day_3_value = horizon_day_3,
                                       rf_3_value = rf_3, var_prob_3_value = var_prob_3,
                                       mat_3_value = mat_3, v0_3_value = v0_3, liq_rate_3_value = liq_rate_3, 
                                       implied_vol_3_value = implied_vol_3, output_file_3 = output_file_3,
                                       table_title = tickers_string_3 + "_" + position_date_3, s0_value = s0,
                                       nstocks_value = nstocks, put0_value = put0, nputs_value = nputs,
                                       VaR_1_value = VaR_1, VaR_2_value = VaR_2, reduction_value = reduction) 
            elif request.form['btn_3'] == 'Download Result Data':
                output_file_3 = request.form["output_file_3"]
                return send_file(output_file_3, mimetype='text/csv', 
                                 attachment_filename=output_file_3.split('/')[1], as_attachment=True)
            else:
                print '3'
        else:
            return render_template('index.html', opt_table_style = 'display:none',
                                   tickers_string_1_value = 'AAPL', position_date_1_value = '2000-12-01',
                                   end_date_1_value = '2016-12-01', v0_1_value = '10000', var_prob_1_value = '0.99',
                                   es_prob_1_value = '0.975', window_year_1_value = '2', horizon_day_1_value = '5',
                                   output_file_1 = 'outputs/price_AAPL_2000-12-01_2016-12-01.csv',
                                   tickers_string_2_value = 'AAPL,MSFT', position_date_2_value = '2000-12-01',
                                   weights_string_2_value = '0.5,0.5', end_date_2_value = '2016-12-01',
                                   v0_2_value = '10000', var_prob_2_value = '0.99', es_prob_2_value = '0.975',
                                   window_year_2_value = '2', horizon_day_2_value = '5',
                                   output_file_2 = 'outputs/price_Portfolio_AAPL_MSFT_2000-12-01_2016-12-01.csv',
                                   tickers_string_3_value = 'AAPL', position_date_3_value = '2016-12-21', 
                                   window_year_3_value = '2', horizon_day_3_value = '5',
                                   rf_3_value = '0.005', var_prob_3_value = '0.99', mat_3_value = '0.5',
                                   v0_3_value = '1000000', liq_rate_3_value = '0.01', implied_vol_3_value = '0.21',
                                   output_file_3 = 'outputs/options_AAPL_2016-12-21.csv')


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)