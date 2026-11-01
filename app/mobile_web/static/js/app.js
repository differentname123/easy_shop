// ==========================================================================
// 移动端电商导购与返利应用前端核心逻辑 (Vue 3 本地化版)
// 适配手机浏览器与微信小程序 web-view
// ==========================================================================

const { createApp, ref, reactive, onMounted, onUnmounted, watch } = Vue;

const PHONE_REGEX = /^1[3-9]\d{9}$/;

const app = createApp({
  setup() {
    // 基础状态
    const currentTab = ref('home'); // 'home' | 'boom' | 'user'
    const toastMessage = ref('');
    const toastTimer = ref(null);
    const isHeaderHidden = ref(false);
    let lastScrollTop = 0;

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
    const homeSearchPage = ref(1);

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
    const isDetailLoading = ref(false);
    const isConverting = ref(false);
    const hasGeneratedLink = ref(false);
    const convertedLinkInfo = ref(null);

    // 渠道授权弹窗状态
    const isAuthModalOpen = ref(false);
    const currentAuthInfo = ref(null);
    const isCheckingAuth = ref(false);

    // 我的返利订单模块状态
    const orderDays = ref(7); // 查询时间跨度：7天 / 30天 / 0(全部)
    const isSyncingOrders = ref(false);
    const userOrdersData = ref({
      total_count: 0,
      total_commission: 0.0,
      settled_commission: 0.0,
      refund_commission: 0.0,
      refund_count: 0,
      order_list: [],
    });

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
          // 登录后拉取一次订单
          syncAndQueryOrders(false);
        } else {
          showToast(data.detail || data.error_msg || '操作失败');
        }
      } catch (err) {
        showToast('网络连接异常');
      } finally {
        isSubmittingAuth.value = false;
      }
    };

    // 彻底修复退出登录：清空所有凭证，刷新绝不重登
    const handleLogout = () => {
      isLoggedIn.value = false;
      userPhone.value = '';
      uid.value = '';
      userProfile.value = {
        uid: '',
        phone: '',
        nickname: '未登录',
        avatar: 'https://img.pddpic.com/mms-avatar/default_avatar.png',
        invite_code: '',
        balance: 0.0,
        total_rebate: 0.0,
        pending_rebate: 0.0,
        auth_status: { pdd: { is_authorized: false } }
      };
      localStorage.removeItem('easy_shop_phone');
      localStorage.removeItem('easy_shop_uid');
      userOrdersData.value = {
        total_count: 0,
        total_commission: 0.0,
        settled_commission: 0.0,
        refund_commission: 0.0,
        refund_count: 0,
        order_list: [],
      };
      showToast('已安全退出登录');
    };

    const fetchUserInfo = async () => {
      const savedPhone = localStorage.getItem('easy_shop_phone');
      // 只有本地真正存有已登录手机号时，才恢复登录状态
      if (!savedPhone) {
        isLoggedIn.value = false;
        return;
      }

      try {
        const res = await fetch(`/api/user/info?phone=${encodeURIComponent(savedPhone)}`);
        const data = await res.json();
        if (res.ok && data.status === 'success' && data.user) {
          userProfile.value = data.user;
          uid.value = data.user.uid;
          userPhone.value = data.user.phone;
          isLoggedIn.value = true;
          localStorage.setItem('easy_shop_uid', data.user.uid);
        } else {
          // 账号失效，清除本地
          handleLogout();
        }
      } catch (err) {
        console.error('获取用户信息失败:', err);
      }
    };

    const handleCheckAndGetAuth = async (platform = 'pdd') => {
      if (!isLoggedIn.value || !uid.value) {
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
        homeSearchPage.value = 1;
        homeGoodsList.value = [];
      }
      homeLoading.value = true;

      try {
        let apiUrl = '';
        if (homeSearchKeyword.value.trim()) {
          apiUrl = `/api/home/search?keyword=${encodeURIComponent(homeSearchKeyword.value.trim())}&page=${homeSearchPage.value}&page_size=20&uid=${encodeURIComponent(uid.value || '')}`;
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
          if (homeSearchKeyword.value.trim()) {
            homeSearchPage.value += 1;
          }
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

    // ==================== 商品详情深度浏览与二级生链 ====================
    const openGoodsDetail = async (item) => {
      // 立即使用已有数据填充，秒显抽屉，消除点击空白感
      const baseImages = (item.carousel_images && item.carousel_images.length) 
        ? item.carousel_images 
        : [item.image_url].filter(Boolean);

      const brandName = item.brand || item.brand_name || '官方甄选';
      const catName = item.category || item.category_name || '精品好物';
      const salesTip = item.sales_tip || '热卖中';

      currentGoods.value = {
        ...item,
        carousel_images: baseImages.length ? baseImages : ['https://img.pddpic.com/mms-avatar/default_avatar.png'],
        service_guarantees: item.service_guarantees || [
          '🛡️ 正品保障 · 假一赔十',
          '📦 退货包运费 · 极速退款',
          '⚡ 48小时极速出库',
          '💰 平台百亿补贴官方特惠'
        ],
        shop_ratings: item.shop_ratings || {
          desc_score: '4.8 (高)',
          serv_score: '4.9 (高)',
          lgst_score: '4.8 (高)'
        },
        specs: item.specs || [
          { label: '商品品牌', value: brandName },
          { label: '所属品类', value: catName },
          { label: '累计销量', value: `已拼 ${salesTip} 件` },
          { label: '发货保障', value: '48小时闪电极速发出 (赠送运费险)' },
          { label: '售后承诺', value: '支持7天无理由退换货 · 假一赔十' }
        ],
        coupon_info: item.coupon_info || {
          valid_tip: '领券后今日内有效，限时限量抢购'
        }
      };

      hasGeneratedLink.value = false;
      convertedLinkInfo.value = null;
      isGoodsModalOpen.value = true;
      isDetailLoading.value = true;

      // 异步获取官方/数据库全量深度详情（多图、实拍轮播、满减券起止与规格参数）
      try {
        const params = new URLSearchParams({
          platform: item.platform || 'pdd',
          goods_sign: item.goods_sign || '',
          goods_id: item.goods_id || item.product_id || '',
          uid: uid.value || '',
        });
        const res = await fetch(`/api/home/detail?${params.toString()}`);
        const data = await res.json();
        if (data.status === 'success' && data.data) {
          const detail = data.data;
          currentGoods.value = {
            ...currentGoods.value,
            ...detail,
            carousel_images: (detail.carousel_images && detail.carousel_images.length) ? detail.carousel_images : currentGoods.value.carousel_images,
            service_guarantees: detail.service_guarantees || currentGoods.value.service_guarantees,
            shop_ratings: detail.shop_ratings || currentGoods.value.shop_ratings,
            specs: detail.specs || currentGoods.value.specs,
            coupon_info: detail.coupon_info || currentGoods.value.coupon_info,
          };
        }
      } catch (e) {
        console.warn('获取商品深度详情失败，保留已有数据:', e);
      } finally {
        isDetailLoading.value = false;
      }
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
          uid: (isLoggedIn.value && uid.value) ? uid.value : null, // 未登录传 null
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
          if (isLoggedIn.value) {
            showToast('已激活并生成专属推手返利链接！');
          } else {
            showToast('已获取官方优惠领券链接！');
          }
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

    const handleJumpToBuy = async () => {
      if (!convertedLinkInfo.value || !convertedLinkInfo.value.final_link) {
        await handleGenerateLink();
      }
      if (convertedLinkInfo.value && convertedLinkInfo.value.final_link) {
        const targetUrl = convertedLinkInfo.value.final_link;
        copyText(targetUrl, '已复制领券链接，正在前往购买...');
        setTimeout(() => {
          window.location.href = targetUrl;
        }, 300);
      } else if (currentGoods.value && (currentGoods.value.product_url || currentGoods.value.promotion_url)) {
        window.location.href = currentGoods.value.product_url || currentGoods.value.promotion_url;
      } else {
        showToast('未获取到商品购买链接');
      }
    };

    const handleCopyPromoteLink = () => {
      // 核心拦截：未登录状态下严禁复制推广链接！
      if (!isLoggedIn.value || !uid.value) {
        showToast('您尚未登录推手账号！无法生成专属返利链接，请先登录绑定手机号');
        goToRegisterFromDetail();
        return;
      }

      if (!convertedLinkInfo.value || !convertedLinkInfo.value.final_link) {
        showToast('正在为您生成专属推手链接...');
        handleGenerateLink().then(() => {
          if (convertedLinkInfo.value && convertedLinkInfo.value.final_link) {
            copyText(convertedLinkInfo.value.final_link, `专属返利推广链接已复制！推手: ${uid.value}，发给好友下单即可赚佣金！`);
          }
        });
        return;
      }

      copyText(convertedLinkInfo.value.final_link, `专属返利推广链接已复制！推手: ${uid.value}，发给好友下单即可赚佣金！`);
    };

    const goToRegisterFromDetail = () => {
      isGoodsModalOpen.value = false;
      currentTab.value = 'user';
      isRegisterMode.value = true;
      showToast('请在此登记手机号，一键成为推手并锁定返利');
    };

    // ==================== 我的返利订单模块 ====================
    const syncAndQueryOrders = async (force = false) => {
      if (!isLoggedIn.value || !uid.value) {
        return;
      }
      isSyncingOrders.value = true;
      try {
        const query = new URLSearchParams({
          uid: uid.value,
          platform: 'pdd',
          days: orderDays.value,
          force: force ? 'true' : 'false',
        });
        const res = await fetch(`/api/order/sync_and_query?${query.toString()}`, {
          method: 'POST',
        });
        const data = await res.json();
        if (data.status === 'success' && data.data) {
          userOrdersData.value = data.data;
          if (force) {
            showToast('已同步最近23小时官方订单！');
          }
        }
      } catch (e) {
        showToast('订单同步异常');
      } finally {
        isSyncingOrders.value = false;
      }
    };

    const switchOrderDays = (d) => {
      orderDays.value = d;
      syncAndQueryOrders(false);
    };

    // ==================== 手势与滚动监听 (智能显隐顶部栏 & 触底自动加载) ====================
    let touchStartY = 0;
    let touchLastY = 0;

    const onTouchStart = (e) => {
      if (e.touches && e.touches[0]) {
        touchStartY = e.touches[0].clientY;
        touchLastY = touchStartY;
      }
    };

    const onTouchMove = (e) => {
      if (e.touches && e.touches[0]) {
        const currentY = e.touches[0].clientY;
        const deltaY = currentY - touchLastY;
        const scrollTop = window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0;

        // 手指往上滑 (deltaY < -6 且离开顶部) -> 立即隐藏顶部栏
        if (deltaY < -6 && scrollTop > 30) {
          isHeaderHidden.value = true;
        } else if (deltaY > 6) {
          // 手指往下滑 -> 立即滑出顶部栏
          isHeaderHidden.value = false;
        }
        touchLastY = currentY;
      }
    };

    const handleGlobalScroll = () => {
      const scrollTop = window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || (document.scrollingElement ? document.scrollingElement.scrollTop : 0);
      const scrollHeight = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, document.scrollingElement ? document.scrollingElement.scrollHeight : 0);
      const clientHeight = window.innerHeight || document.documentElement.clientHeight || document.body.clientHeight;

      // 1. 顶部栏智能显隐：根据滚动方向判定
      const diff = scrollTop - lastScrollTop;
      if (scrollTop > 40) {
        if (diff > 6) {
          isHeaderHidden.value = true;
        } else if (diff < -6) {
          isHeaderHidden.value = false;
        }
      } else {
        isHeaderHidden.value = false;
      }
      lastScrollTop = scrollTop <= 0 ? 0 : scrollTop;

      // 2. 距底部 350px 自动无缝触底加载下一页商品
      if (scrollHeight - scrollTop - clientHeight < 350) {
        if (currentTab.value === 'home' && !homeLoading.value) {
          loadHomeGoods(false);
        } else if (currentTab.value === 'boom' && !boomLoading.value && boomHasMore.value) {
          loadBoomGoods(false);
        }
      }
    };

    let bottomObserver = null;
    const setupBottomObserver = () => {
      if (typeof IntersectionObserver === 'undefined') return;
      if (bottomObserver) bottomObserver.disconnect();
      bottomObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
          if (entry.isIntersecting) {
            if (currentTab.value === 'home' && !homeLoading.value) {
              loadHomeGoods(false);
            } else if (currentTab.value === 'boom' && !boomLoading.value && boomHasMore.value) {
              loadBoomGoods(false);
            }
          }
        });
      }, { rootMargin: '300px' });

      setTimeout(() => {
        const homeSen = document.getElementById('homeBottomSentinel');
        const boomSen = document.getElementById('boomBottomSentinel');
        if (homeSen) bottomObserver.observe(homeSen);
        if (boomSen) bottomObserver.observe(boomSen);
      }, 300);
    };

    // 页面初始化
    onMounted(async () => {
      // 提取小程序透传的 URL 参数
      const urlParams = new URLSearchParams(window.location.search);
      const urlUid = urlParams.get('uid');
      if (urlUid) {
        uid.value = urlUid;
      }
      const urlInvite = urlParams.get('invite_code');
      if (urlInvite) {
        formInviteCode.value = urlInvite;
      }

      await fetchUserInfo();
      loadHomeChannels();
      loadHomeGoods(true);

      // 绑定全局手势与多源滚动监听
      window.addEventListener('scroll', handleGlobalScroll, { passive: true });
      document.addEventListener('scroll', handleGlobalScroll, { passive: true });
      window.addEventListener('touchstart', onTouchStart, { passive: true });
      window.addEventListener('touchmove', onTouchMove, { passive: true });
      setupBottomObserver();
    });

    onUnmounted(() => {
      window.removeEventListener('scroll', handleGlobalScroll);
      document.removeEventListener('scroll', handleGlobalScroll);
      window.removeEventListener('touchstart', onTouchStart);
      window.removeEventListener('touchmove', onTouchMove);
      if (bottomObserver) bottomObserver.disconnect();
    });

    // 监听 Tab 切换
    watch(currentTab, (newTab) => {
      // 切换 Tab 时重置顶部栏显隐
      isHeaderHidden.value = false;
      if (newTab === 'boom' && boomGoodsList.value.length === 0) {
        loadBoomGoods(true);
      } else if (newTab === 'user') {
        fetchUserInfo();
        if (isLoggedIn.value) {
          syncAndQueryOrders(false);
        }
      }
    });

    return {
      currentTab,
      toastMessage,
      isHeaderHidden,
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
      isDetailLoading,
      isConverting,
      hasGeneratedLink,
      convertedLinkInfo,
      openGoodsDetail,
      handleGenerateLink,
      handleJumpToBuy,
      handleCopyPromoteLink,
      goToRegisterFromDetail,
      // 授权
      isAuthModalOpen,
      currentAuthInfo,
      isCheckingAuth,
      handleCheckAndGetAuth,
      // 订单模块
      orderDays,
      isSyncingOrders,
      userOrdersData,
      syncAndQueryOrders,
      switchOrderDays,
      // 工具
      copyText,
      showToast,
    };
  }
});

app.mount('#app');
