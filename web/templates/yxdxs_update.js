// 阴线都向上页面 - 优化的动态刷新逻辑
// 支持开盘时间1秒刷新，非开盘时间3秒刷新

let currentRefreshInterval = 3000; // 默认3秒
let updateTimer = null;
let realtimeTimer = null;
let lastTimestamp = null;

// 动态更新刷新间隔
function updateRefreshInterval(newInterval) {
    if (newInterval && currentRefreshInterval !== newInterval) {
        console.log(`⏱️ 刷新间隔变更: ${currentRefreshInterval/1000}秒 → ${newInterval/1000}秒`);
        currentRefreshInterval = newInterval * 1000; // 转换为毫秒
        
        // 重启定时器
        restartTimers();
    }
}

// 重启所有定时器
function restartTimers() {
    if (updateTimer) {
        clearInterval(updateTimer);
    }
    if (realtimeTimer) {
        clearInterval(realtimeTimer);
    }
    
    // 启动新的定时器
    startDataUpdateCheck();
    startRealtimeRefresh();
}

// 检查数据更新
async function checkDataUpdate() {
    try {
        const response = await fetch('/api/yxdxs/check_update');
        const result = await response.json();
        
        if (result.success) {
            // 更新刷新间隔
            if (result.refresh_interval) {
                updateRefreshInterval(result.refresh_interval);
            }
            
            // 如果有更新或时间戳变化
            if (result.updated && result.timestamp !== lastTimestamp) {
                console.log(`🔄 检测到数据更新，重新加载... (${result.count}只股票)`);
                lastTimestamp = result.timestamp;
                await loadStocksData();
            }
        }
    } catch (error) {
        console.error('检查更新失败:', error);
    }
}

// 启动数据更新检查定时器
function startDataUpdateCheck() {
    updateTimer = setInterval(checkDataUpdate, currentRefreshInterval);
    console.log(`✅ 数据更新检查已启动 (间隔: ${currentRefreshInterval/1000}秒)`);
}

// 加载股票数据
async function loadStocksData() {
    try {
        const startTime = performance.now();
        const response = await fetch('/api/yxdxs/stocks');
        const result = await response.json();
        
        if (result.success) {
            const loadTime = performance.now() - startTime;
            
            // 显示性能指标
            if (result.performance) {
                console.log(`📊 性能指标:`);
                console.log(`  - 总耗时: ${result.performance.total_time}`);
                console.log(`  - 网络延迟: ${result.performance.network_latency}`);
                console.log(`  - SCAN耗时: ${result.performance.scan_time}`);
                console.log(`  - 解析耗时: ${result.performance.parse_time}`);
                console.log(`  - 前端耗时: ${loadTime.toFixed(2)}ms`);
                console.log(`  - 股票数量: ${result.performance.count}`);
            }
            
            // 更新刷新间隔
            if (result.refresh_interval) {
                updateRefreshInterval(result.refresh_interval);
            }
            
            // 渲染股票列表
            renderStocks(result.data);
            
            // 更新统计信息
            updateStats(result.data);
        }
    } catch (error) {
        console.error('加载股票数据失败:', error);
    }
}

// 刷新实时行情
async function refreshRealtime() {
    const stockCodes = getAllStockCodes();
    if (stockCodes.length === 0) return;
    
    try {
        const response = await fetch('/api/yxdxs/realtime', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ codes: stockCodes })
        });
        
        const result = await response.json();
        if (result.success) {
            updateRealtimeData(result.data);
        }
    } catch (error) {
        console.error('刷新实时行情失败:', error);
    }
}

// 启动实时行情刷新定时器
function startRealtimeRefresh() {
    realtimeTimer = setInterval(refreshRealtime, currentRefreshInterval);
    console.log(`✅ 实时行情刷新已启动 (间隔: ${currentRefreshInterval/1000}秒)`);
}

// 渲染股票列表（需要根据实际HTML结构实现）
function renderStocks(stocks) {
    // TODO: 实现具体的股票列表渲染逻辑
    console.log(`渲染 ${stocks.length} 只股票`);
}

// 更新统计信息
function updateStats(stocks) {
    const total = stocks.length;
    const up = stocks.filter(s => s.today_change > 0).length;
    const down = stocks.filter(s => s.today_change < 0).length;
    
    // TODO: 更新页面上的统计显示
    console.log(`统计: 总${total}只, 上涨${up}只, 下跌${down}只`);
}

// 获取所有股票代码
function getAllStockCodes() {
    // TODO: 从DOM中提取所有股票代码
    return [];
}

// 更新实时数据
function updateRealtimeData(data) {
    // TODO: 更新页面上的实时涨跌幅
    console.log(`更新${Object.keys(data).length}只股票的实时数据`);
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', async () => {
    console.log('🚀 阴线都向上页面初始化...');
    
    // 首次加载数据
    await loadStocksData();
    
    // 启动定时检查
    await checkDataUpdate();
    startDataUpdateCheck();
    
    // 启动实时刷新
    startRealtimeRefresh();
    
    console.log('✅ 初始化完成');
});

// 页面卸载时清理定时器
window.addEventListener('beforeunload', () => {
    if (updateTimer) clearInterval(updateTimer);
    if (realtimeTimer) clearInterval(realtimeTimer);
});
