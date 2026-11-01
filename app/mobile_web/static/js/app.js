// ==========================================================================
// 移动端电商导购与返利应用前端核心逻辑 (Vue 3)
// 适配手机浏览器与微信小程序 web-view
// ==========================================================================

const { createApp, ref, reactive, onMounted, watch } = Vue;

const app = createApp({
  setup() {
    // 基础状态
    const currentTab = ref('home'); // 'home' | 'boom' | 'user'
    const toastMessage = ref('');
    const toastTimer = ref(null);

    // 用户推手状态
    const uid = ref(localStorage.getItem('easy_shop_uid') || '');
    const userProfile = ref({
      uid: '',
      nickname: '加载中...',
      avatar: 'https://img.pddpic.com/mms-avatar/default_avatar.png',
      balance: 0.0,
      total_rebate: 0.0,
      pending_rebate: 0.0,
      auth_status: {
        pdd: { is_authorized: false, bind: 0 },
        taobao: { is_authorized: false, bind: 0 },
        jd: { is_authorized: false, bind: 0 },
      }
    });

    // 首页状态 (拼多多多多进宝推荐)
    const homeChannels = ref([
      { channel_type: 5, name: '实时热销' },
      { channel_type: 1, name: '今日爆款' },
      { channel_type: 6, name: '百亿补贴' },
      { channel_type: 3, name: '高佣优选' },
      { channel_type: 4, name: '秒杀抢购' },
    ]);
    const currentChannel = ref(5);
    const homeSearchKeyword = ref('');
    const homeGoodsList = ref([]);
    const homeLoading = ref(false);
    const homeOffset = ref(0);

    // 爆单推广状态 (数据库存量爆品库)
    const boomPlatforms = ref([
      { code: 'all', name: '全部' },
      { code: 'pdd', name: '拼多多' },
      { code: 'taobao', name: '淘宝' },
      { code: 'jd', name: '京东' },
    ]);
    const boomPlatform = ref('all');
    const boomCategories = ref(['全部']);
    const boomCategory = ref('全部');
    const boomSortBy = ref('latest');
    const boomGoodsList = ref([]);
    const boomPage = ref(1);
    const boomLoading = ref(false);
    const boomHasMore = ref(true);

    // 商品领券/购买弹窗状态
    const isGoodsModalOpen = ref(false);
    const currentGoods = ref(null);
    const isConverting = ref(false);
    const convertedLinkInfo = ref(null);

    // 授权引导弹窗状态
    const isAuthModalOpen = ref(false);
    const currentAuthInfo = ref(null);
    const isCheckingAuth = ref(false);

    // ==================== 工具函数 ====================
    const showToast = (msg, duration = 2000) => {
      toastMessage.value = msg;
      if (toastTimer.value) clearTimeout(toastTimer.value);
      toastTimer.value = setTimeout(() => {
        toastMessage.value = '';
      }, duration);
    };

    const copyText = (text, successTip = '已复制到剪贴板') => {
      if (!text) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
          showToast(successTip);
        }).catch(() => {
          fallbackCopy(text, successTip);
        });
      } else {
        fallbackCopy(text, successTip);
      }
    };

    const fallbackCopy = (text, successTip) => {
      const input = document.createElement('textarea');
      input.value = text;
      document.body.appendChild(input);
      input.select();
      try {
        document.execCommand('copy');
        showToast(successTip);
      } catch (e) {
        showToast('复制失败，请手动长按复制');
      }
      document.body.removeChild(input);
    };

    // ==================== 用户模块 ====================
    const initUser = async () => {
      // 1. 优先提取 URL 中附带的 uid 参数（便于微信小程序跳转直接透传）
      const urlParams = new URLSearchParams(window.location.search);
      const urlUid = urlParams.get('uid');
      if (urlUid) {
        uid.value = urlUid;
        localStorage.setItem('easy_shop_uid', urlUid);
      }

      try {
        const res = await fetch('/api/user/init', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ uid: uid.value || null }),
        });
        const data = await res.json();
        if (data.status === 'success' && data.user) {
          uid.value = data.user.uid;
          localStorage.setItem('easy_shop_uid', data.user.uid);
          userProfile.value = data.user;
        }
      } catch (err) {
        console.error('初始化用户异常:', err);
      }
    };

    const fetchUserInfo = async () => {
      if (!uid.value) return;
      try {
        const res = await fetch(`/api/user/info?uid=${encodeURIComponent(uid.value)}`);
        const data = await res.json();
        if (data.status === 'success' && data.user) {
          userProfile.value = data.user;
        }
      } catch (err) {
        console.error('获取用户信息失败:', err);
      }
    };

    const handleCheckAndGetAuth = async (platform = 'pdd') => {
      if (!uid.value) {
        showToast('用户状态异常，请刷新页面');
        return;
      }
      isCheckingAuth.value = true;
      try {
        const res = await fetch(`/api/user/auth?uid=${encodeURIComponent(uid.value)}&platform=${platform}`);
        const data = await res.json();
        if (data.status === 'success') {
          currentAuthInfo.value = data;
          isAuthModalOpen.value = true;
          // 重新更新用户授权状态
          fetchUserInfo();
          if (data.is_authorized) {
            showToast(`${data.platform_name} 已成功授权！`);
          }
        } else {
          showToast(data.error_msg || '获取授权状态失败');
        }
      } catch (err) {
        showToast('网络请求异常');
      } finally {
        isCheckingAuth.value = false;
      }
    };

    // ==================== 首页模块 (拼多多推荐) ====================
    const loadHomeChannels = async () => {
      try {
        const res = await fetch('/api/home/channels');
        const data = await res.json();
        if (data.status === 'success' && data.data) {
          homeChannels.value = data.data;
        }
      } catch (e) {
        console.warn('加载频道失败:', e);
      }
    };

    const loadHomeGoods = async (isReset = false) => {
      if (homeLoading.value) return;
      if (isReset) {
        homeOffset.value = 0;
        homeGoodsList.value = [];
      }
      homeLoading.value = true;

      try {
        let apiUrl = '';
        if (homeSearchKeyword.value.trim()) {
          apiUrl = `/api/home/search?keyword=${encodeURIComponent(homeSearchKeyword.value.trim())}&page=1&page_size=30&uid=${encodeURIComponent(uid.value || '')}`;
        } else {
          apiUrl = `/api/home/recommend?channel_type=${currentChannel.value}&offset=${homeOffset.value}&limit=20&uid=${encodeURIComponent(uid.value || '')}`;
        }

        const res = await fetch(apiUrl);
        const data = await res.json();
        if (data.status === 'success' && Array.isArray(data.data)) {
          if (isReset) {
            homeGoodsList.value = data.data;
          } else {
            homeGoodsList.value.push(...data.data);
          }
          homeOffset.value += data.data.length;
        } else {
          if (isReset) homeGoodsList.value = [];
          if (data.error_msg) showToast(data.error_msg);
        }
      } catch (err) {
        showToast('加载商品失败，请检查网络');
      } finally {
        homeLoading.value = false;
      }
    };

    const onSearchHome = () => {
      loadHomeGoods(true);
    };

    const switchChannel = (type) => {
      currentChannel.value = type;
      homeSearchKeyword.value = '';
      loadHomeGoods(true);
    };

    // ==================== 爆单推广模块 (数据库商品) ====================
    const loadBoomCategories = async () => {
      try {
        const res = await fetch(`/api/goods/categories?platform=${boomPlatform.value}`);
        const data = await res.json();
        if (data.status === 'success' && data.data) {
          boomCategories.value = ['全部', ...data.data];
        }
      } catch (e) {
        boomCategories.value = ['全部'];
      }
    };

    const loadBoomGoods = async (isReset = false) => {
      if (boomLoading.value) return;
      if (isReset) {
        boomPage.value = 1;
        boomGoodsList.value = [];
        boomHasMore.value = true;
      }
      if (!boomHasMore.value && !isReset) return;

      boomLoading.value = true;
      try {
        const queryParams = new URLSearchParams({
          platform: boomPlatform.value,
          category: boomCategory.value === '全部' ? '' : boomCategory.value,
          sort_by: boomSortBy.value,
          page: boomPage.value,
          page_size: 20,
        });

        const res = await fetch(`/api/goods/list?${queryParams.toString()}`);
        const data = await res.json();
        if (data.status === 'success' && Array.isArray(data.data)) {
          if (isReset) {
            boomGoodsList.value = data.data;
          } else {
            boomGoodsList.value.push(...data.data);
          }
          boomHasMore.value = data.has_more;
          boomPage.value += 1;
        } else {
          if (isReset) boomGoodsList.value = [];
          boomHasMore.value = false;
        }
      } catch (err) {
        showToast('加载爆品失败');
      } finally {
        boomLoading.value = false;
      }
    };

    const switchBoomPlatform = (code) => {
      boomPlatform.value = code;
      boomCategory.value = '全部';
      loadBoomCategories();
      loadBoomGoods(true);
    };

    const switchBoomCategory = (cat) => {
      boomCategory.value = cat;
      loadBoomGoods(true);
    };

    const switchBoomSort = (sort) => {
      boomSortBy.value = sort;
      loadBoomGoods(true);
    };

    // ==================== 商品详情 & 专属转链生链 ====================
    const openGoodsDetail = async (item) => {
      currentGoods.value = item;
      convertedLinkInfo.value = null;
      isGoodsModalOpen.value = true;
      isConverting.value = true;

      try {
        const payload = {
          platform: item.platform || 'pdd',
          goods_sign: item.goods_sign || '',
          goods_id: item.goods_id || item.product_id || '',
          product_url: item.product_url || '',
          uid: uid.value, // 自动注入当前用户专属 UID 进行佣金归因
        };

        const res = await fetch('/api/promote/convert', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (data.status === 'success') {
          convertedLinkInfo.value = data;
        } else {
          convertedLinkInfo.value = {
            error: data.error_msg || '转链失败',
            short_url: item.product_url || item.promotion_url || ''
          };
        }
      } catch (e) {
        convertedLinkInfo.value = {
          error: '网络异常，转链失败',
          short_url: item.product_url || ''
        };
      } finally {
        isConverting.value = false;
      }
    };

    const handleJumpToBuy = () => {
      if (!convertedLinkInfo.value) return;
      const targetUrl = convertedLinkInfo.value.mobile_short_url
        || convertedLinkInfo.value.short_url
        || convertedLinkInfo.value.mobile_url
        || convertedLinkInfo.value.url;

      if (!targetUrl) {
        showToast('推广链接未就绪');
        return;
      }

      // 自动复制该链接以便在微信粘贴，同时尝试跳转
      copyText(targetUrl, '已复制领券链接，正在前往...');
      setTimeout(() => {
        window.location.href = targetUrl;
      }, 300);
    };

    // 页面挂载初始化
    onMounted(async () => {
      await initUser();
      loadHomeChannels();
      loadHomeGoods(true);
    });

    // 监听 Tab 切换
    watch(currentTab, (newTab) => {
      if (newTab === 'boom' && boomGoodsList.value.length === 0) {
        loadBoomCategories();
        loadBoomGoods(true);
      } else if (newTab === 'user') {
        fetchUserInfo();
      }
    });

    return {
      currentTab,
      toastMessage,
      uid,
      userProfile,
      // 首页
      homeChannels,
      currentChannel,
      homeSearchKeyword,
      homeGoodsList,
      homeLoading,
      switchChannel,
      onSearchHome,
      loadHomeGoods,
      // 爆单
      boomPlatforms,
      boomPlatform,
      boomCategories,
      boomCategory,
      boomSortBy,
      boomGoodsList,
      boomLoading,
      boomHasMore,
      switchBoomPlatform,
      switchBoomCategory,
      switchBoomSort,
      loadBoomGoods,
      // 弹窗与交互
      isGoodsModalOpen,
      currentGoods,
      isConverting,
      convertedLinkInfo,
      openGoodsDetail,
      handleJumpToBuy,
      // 授权
      isAuthModalOpen,
      currentAuthInfo,
      isCheckingAuth,
      handleCheckAndGetAuth,
      // 通用
      copyText,
      showToast,
    };
  }
});

app.mount('#app');
