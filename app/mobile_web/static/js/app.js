// ==========================================================================
// 移动端电商导购与返利应用前端核心逻辑 (Vue 3 本地化版)
// 适配手机浏览器与微信小程序 web-view
// ==========================================================================

const { createApp, ref, reactive, onMounted, watch } = Vue;

const PHONE_REGEX = /^1[3-9]\d{9}$/;

const app = createApp({
  setup() {
    // 基础状态
    const currentTab = ref('home'); // 'home' | 'boom' | 'user'
    const toastMessage = ref('');
    const toastTimer = ref(null);

    // 用户认证与推手状态
    const isLoggedIn = ref(false);
    const uid = ref(localStorage.getItem('easy_shop_uid') || '');
    const userPhone = ref(localStorage.getItem('easy_shop_phone') || '');
    const userProfile = ref({
      uid: '',
      phone: '',
      nickname: '未登录',
      avatar: 'https://img.pddpic.com/mms-avatar/default_avatar.png',
      invite_code: '',
      invited_by: null,
      balance: 0.0,
      total_rebate: 0.0,
      pending_rebate: 0.0,
      auth_status: {
        pdd: { is_authorized: false, bind: 0 },
        taobao: { is_authorized: false, bind: 0 },
        jd: { is_authorized: false, bind: 0 },
      }
    });

    // 登录/注册表单
    const isRegisterMode = ref(true);
    const formPhone = ref('');
    const formPassword = ref('');
    const formNickname = ref('');
    const formInviteCode = ref('');
    const isSubmittingAuth = ref(false);

    // 首页状态 (拼多多进宝官方推荐)
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
    const boomSearchKeyword = ref('');
    const boomSortBy = ref('latest');
    const boomGoodsList = ref([]);
    const boomPage = ref(1);
    const boomLoading = ref(false);
    const boomHasMore = ref(true);

    // 商品详情与专属生链状态
    const isGoodsModalOpen = ref(false);
    const currentGoods = ref(null);
    const activeCarouselIdx = ref(0);
    const isConverting = ref(false);
    const hasGeneratedLink = ref(false);
    const convertedLinkInfo = ref(null);

    // 渠道授权弹窗状态
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
        showToast('复制失败，请长按手动复制');
      }
      document.body.removeChild(input);
    };

    const formatPhone = (p) => {
      if (!p || p.length !== 11) return p || '';
      return p.substring(0, 3) + '****' + p.substring(7);
    };

    // ==================== 用户认证与登录 ====================
    const handleRegisterOrLogin = async () => {
      const phone = formPhone.value.trim();
      const pwd = formPassword.value.trim();

      if (!phone) {
        showToast('请输入手机号');
        return;
      }
      if (!PHONE_REGEX.test(phone)) {
        showToast('请输入有效的11位中国大陆手机号');
        return;
      }
      if (!pwd || pwd.length < 6) {
        showToast('密码长度不能少于6位');
        return;
      }

      isSubmittingAuth.value = true;
      try {
        const payload = {
          phone: phone,
          password: pwd,
          nickname: formNickname.value.trim() || null,
          invite_code: formInviteCode.value.trim() || null,
        };

        const res = await fetch('/api/user/register_or_login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.ok && data.status === 'success' && data.user) {
          isLoggedIn.value = true;
          uid.value = data.user.uid;
          userPhone.value = data.user.phone;
          userProfile.value = data.user;

          localStorage.setItem('easy_shop_uid', data.user.uid);
          localStorage.setItem('easy_shop_phone', data.user.phone);

          showToast(isRegisterMode.value ? '注册并登录成功！' : '登录成功！');
          formPassword.value = '';
        } else {
          showToast(data.detail || data.error_msg || '操作失败');
        }
      } catch (err) {
        showToast('网络连接异常');
      } finally {
        isSubmittingAuth.value = false;
      }
    };

    const handleLogout = () => {
      isLoggedIn.value = false;
      userPhone.value = '';
      localStorage.removeItem('easy_shop_phone');
      showToast('已安全退出登录');
    };

    const fetchUserInfo = async () => {
      if (!uid.value && !userPhone.value) return;
      try {
        const query = userPhone.value ? `phone=${encodeURIComponent(userPhone.value)}` : `uid=${encodeURIComponent(uid.value)}`;
        const res = await fetch(`/api/user/info?${query}`);
        const data = await res.json();
        if (res.ok && data.status === 'success' && data.user) {
          userProfile.value = data.user;
          uid.value = data.user.uid;
          if (data.user.phone) {
            isLoggedIn.value = true;
            userPhone.value = data.user.phone;
          }
        }
      } catch (err) {
        console.error('获取用户信息失败:', err);
      }
    };

    const handleCheckAndGetAuth = async (platform = 'pdd') => {
      if (!uid.value) {
        showToast('请先完成手机号登记注册');
        return;
      }
      isCheckingAuth.value = true;
      try {
        const res = await fetch(`/api/user/auth?uid=${encodeURIComponent(uid.value)}&platform=${platform}`);
        const data = await res.json();
        if (data.status === 'success') {
          currentAuthInfo.value = data;
          isAuthModalOpen.value = true;
          fetchUserInfo();
          if (data.is_authorized) {
            showToast(`${data.platform_name} 已成功备案！`);
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
          apiUrl = `/api/home/search?keyword=${encodeURIComponent(homeSearchKeyword.value.trim())}&page=1&page_size=20&uid=${encodeURIComponent(uid.value || '')}`;
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
        }
      } catch (err) {
        showToast('商品加载中断，请重试');
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
          keyword: boomSearchKeyword.value.trim(),
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

    const onSearchBoom = () => {
      loadBoomGoods(true);
    };

    const switchBoomPlatform = (code) => {
      boomPlatform.value = code;
      loadBoomGoods(true);
    };

    const switchBoomSort = (sort) => {
      boomSortBy.value = sort;
      loadBoomGoods(true);
    };

    // ==================== 商品详情与二级专属转链生链 ====================
    const openGoodsDetail = (item) => {
      currentGoods.value = item;
      activeCarouselIdx.value = 0;
      hasGeneratedLink.value = false;
      convertedLinkInfo.value = null;
      isGoodsModalOpen.value = true;
    };

    const handleGenerateLink = async () => {
      if (!currentGoods.value) return;
      isConverting.value = true;

      try {
        const payload = {
          platform: currentGoods.value.platform || 'pdd',
          goods_sign: currentGoods.value.goods_sign || '',
          goods_id: currentGoods.value.goods_id || currentGoods.value.product_id || '',
          product_url: currentGoods.value.product_url || '',
          uid: uid.value || null,
        };

        const res = await fetch('/api/promote/convert', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (data.status === 'success') {
          convertedLinkInfo.value = data;
          hasGeneratedLink.value = true;
          showToast('已生成您的专属推广返利链接！');
        } else {
          convertedLinkInfo.value = {
            final_link: currentGoods.value.product_url || currentGoods.value.promotion_url || '',
            error: data.error_msg || '转链失败'
          };
          hasGeneratedLink.value = true;
        }
      } catch (e) {
        showToast('生成专属链接异常');
      } finally {
        isConverting.value = false;
      }
    };

    const handleJumpToBuy = () => {
      if (!convertedLinkInfo.value || !convertedLinkInfo.value.final_link) {
        showToast('推广链接未就绪');
        return;
      }
      const targetUrl = convertedLinkInfo.value.final_link;
      // 统一复制与跳转同一链接
      copyText(targetUrl, '已复制领券链接，正在前往...');
      setTimeout(() => {
        window.location.href = targetUrl;
      }, 300);
    };

    const handleCopyPromoteLink = () => {
      if (!convertedLinkInfo.value || !convertedLinkInfo.value.final_link) {
        showToast('请先点击生成专属链接');
        return;
      }
      copyText(convertedLinkInfo.value.final_link, '专属返利推广链接已复制，可分享赚佣！');
    };

    // 页面初始化
    onMounted(async () => {
      // 提取小程序透传的 URL 参数
      const urlParams = new URLSearchParams(window.location.search);
      const urlUid = urlParams.get('uid');
      if (urlUid) {
        uid.value = urlUid;
        localStorage.setItem('easy_shop_uid', urlUid);
      }
      const urlInvite = urlParams.get('invite_code');
      if (urlInvite) {
        formInviteCode.value = urlInvite;
      }

      await fetchUserInfo();
      loadHomeChannels();
      loadHomeGoods(true);
    });

    // 监听 Tab 切换
    watch(currentTab, (newTab) => {
      if (newTab === 'boom' && boomGoodsList.value.length === 0) {
        loadBoomGoods(true);
      } else if (newTab === 'user') {
        fetchUserInfo();
      }
    });

    return {
      currentTab,
      toastMessage,
      uid,
      userPhone,
      isLoggedIn,
      userProfile,
      formatPhone,
      // 注册与登录
      isRegisterMode,
      formPhone,
      formPassword,
      formNickname,
      formInviteCode,
      isSubmittingAuth,
      handleRegisterOrLogin,
      handleLogout,
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
      boomSearchKeyword,
      boomSortBy,
      boomGoodsList,
      boomLoading,
      boomHasMore,
      switchBoomPlatform,
      switchBoomSort,
      onSearchBoom,
      loadBoomGoods,
      // 商品详情与生链
      isGoodsModalOpen,
      currentGoods,
      activeCarouselIdx,
      isConverting,
      hasGeneratedLink,
      convertedLinkInfo,
      openGoodsDetail,
      handleGenerateLink,
      handleJumpToBuy,
      handleCopyPromoteLink,
      // 授权
      isAuthModalOpen,
      currentAuthInfo,
      isCheckingAuth,
      handleCheckAndGetAuth,
      // 工具
      copyText,
      showToast,
    };
  }
});

app.mount('#app');
