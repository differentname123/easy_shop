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

    // 智能省钱加载器与话术轮播控制器
    const smartLoading = ref({
      visible: false,
      title: '正在搜寻专属优惠与底价...',
      currentSlogan: '正在向拼多多官方申请大额隐藏券...',
      fadeClass: 'fade-in'
    });
    let sloganTimer = null;
    let sloganIndex = 0;

    const startSmartLoading = (title, slogans) => {
      smartLoading.value.visible = true;
      smartLoading.value.title = title || '正在智能比价与核验福利...';
      const sloganList = (slogans && slogans.length) ? slogans : [
        '正在向拼多多官方申请大额隐藏券...',
        '正在全网比价，锁定最低到手折扣...',
        '专属活动补贴匹配中，校验优惠有效性...',
        '正在为您呈现实时优惠与高清实拍...'
      ];
      sloganIndex = 0;
      smartLoading.value.currentSlogan = sloganList[0];
      smartLoading.value.fadeClass = 'fade-in';

      if (sloganTimer) clearInterval(sloganTimer);
      sloganTimer = setInterval(() => {
        smartLoading.value.fadeClass = 'fade-out';
        setTimeout(() => {
          sloganIndex = (sloganIndex + 1) % sloganList.length;
          smartLoading.value.currentSlogan = sloganList[sloganIndex];
          smartLoading.value.fadeClass = 'fade-in';
        }, 220);
      }, 1300);
    };

    const stopSmartLoading = () => {
      smartLoading.value.visible = false;
      if (sloganTimer) {
        clearInterval(sloganTimer);
        sloganTimer = null;
      }
    };


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
          if (data.user.auth_token) {
            localStorage.setItem('easy_shop_token', data.user.auth_token);
          }

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
      localStorage.removeItem('easy_shop_token');
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
      const savedToken = localStorage.getItem('easy_shop_token');
      // 必须有已存储的 token 才能恢复会话
      if (!savedToken) {
        if (localStorage.getItem('easy_shop_phone')) {
          handleLogout();
        } else {
          isLoggedIn.value = false;
        }
        return;
      }

      try {
        const res = await fetch('/api/user/info', {
          headers: {
            'Authorization': `Bearer ${savedToken}`
          }
        });
        const data = await res.json();
        if (res.ok && data.status === 'success' && data.user) {
          userProfile.value = data.user;
          uid.value = data.user.uid;
          userPhone.value = data.user.phone;
          isLoggedIn.value = true;
          localStorage.setItem('easy_shop_uid', data.user.uid);
          if (data.user.auth_token) {
            localStorage.setItem('easy_shop_token', data.user.auth_token);
          }
        } else {
          // 凭证失效，清除本地并退出登录
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
      const token = localStorage.getItem('easy_shop_token');
      if (!token) {
        handleLogout();
        showToast('登录凭证已过期，请重新登录');
        return;
      }
      isCheckingAuth.value = true;
      try {
        const res = await fetch(`/api/user/auth?platform=${platform}`, {
          headers: {
            'Authorization': `Bearer ${token}`
          }
        });
        const data = await res.json();
        if (data.status === 'success') {
          currentAuthInfo.value = data;
          isAuthModalOpen.value = true;
          fetchUserInfo();
          if (data.is_authorized) {
            showToast(`${data.platform_name} 已成功备案！`);
          }
        } else {
          if (res.status === 401) {
            handleLogout();
            showToast('登录凭证已过期，请重新登录');
          } else {
            showToast(data.error_msg || data.detail || '获取授权状态失败');
          }
        }
      } catch (err) {
        showToast('网络请求异常');
      } finally {
        isCheckingAuth.value = false;
      }
    };

    // ==================== 首页模块 (拼多多推荐) ====================
    // 频道数据快速缓存：channel_type -> { list: [], offset: number }，实现秒切 0ms 瞬间响应
    const channelCache = {};

    const loadHomeChannels = async () => {
      try {
        const cached = sessionStorage.getItem('easy_shop_home_channels');
        if (cached) {
          const parsed = JSON.parse(cached);
          if (Array.isArray(parsed) && parsed.length > 0) {
            homeChannels.value = parsed;
          }
        }
        const res = await fetch('/api/home/channels');
        const data = await res.json();
        if (data.status === 'success' && data.data) {
          homeChannels.value = data.data;
          sessionStorage.setItem('easy_shop_home_channels', JSON.stringify(data.data));
        }
      } catch (e) {
        console.warn('加载频道失败:', e);
      }
    };

    const syncHomeSearchToUrl = (kw) => {
      try {
        const url = new URL(window.location.href);
        if (kw && kw.trim()) {
          url.searchParams.set('keyword', kw.trim());
          sessionStorage.setItem('easy_shop_home_search_keyword', kw.trim());
        } else {
          url.searchParams.delete('keyword');
          sessionStorage.removeItem('easy_shop_home_search_keyword');
          sessionStorage.removeItem('easy_shop_home_search_state');
        }
        window.history.replaceState({ keyword: kw ? kw.trim() : '', tab: 'home' }, '', url.toString());
      } catch (e) {}
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
        const kw = homeSearchKeyword.value.trim();
        if (kw) {
          apiUrl = `/api/home/search?keyword=${encodeURIComponent(kw)}&page=${homeSearchPage.value}&page_size=20&uid=${encodeURIComponent(uid.value || '')}`;
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
          if (kw) {
            homeSearchPage.value += 1;
            // 实时保存当前搜索快照至 sessionStorage
            try {
              sessionStorage.setItem('easy_shop_home_search_state', JSON.stringify({
                keyword: kw,
                list: homeGoodsList.value,
                page: homeSearchPage.value,
                offset: homeOffset.value,
                scrollTop: window.pageYOffset || document.documentElement.scrollTop || 0
              }));
            } catch (e) {}
          } else {
            // 同步记录当前频道已加载内容到缓存
            channelCache[currentChannel.value] = {
              list: [...homeGoodsList.value],
              offset: homeOffset.value,
            };
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
      const kw = homeSearchKeyword.value.trim();
      syncHomeSearchToUrl(kw);
      loadHomeGoods(true);
    };

    const clearHomeSearch = () => {
      homeSearchKeyword.value = '';
      syncHomeSearchToUrl('');
      loadHomeGoods(true);
    };

    const switchChannel = (type) => {
      // 记录离开前频道的已有数据
      if (!homeSearchKeyword.value.trim() && homeGoodsList.value.length > 0) {
        channelCache[currentChannel.value] = {
          list: [...homeGoodsList.value],
          offset: homeOffset.value,
        };
      }

      currentChannel.value = type;
      homeSearchKeyword.value = '';
      syncHomeSearchToUrl('');

      // 若目标频道已有数据缓存，直接 0ms 瞬间还原，无需白屏重拉
      if (channelCache[type] && channelCache[type].list && channelCache[type].list.length > 0) {
        homeGoodsList.value = [...channelCache[type].list];
        homeOffset.value = channelCache[type].offset;
        return;
      }

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

    // ==================== 商品跳转至独立通用全屏详情大页 ====================
    const openGoodsDetail = (item) => {
      // 预置卡片简要数据到 sessionStorage，实现详情页 0ms 瞬间无白屏秒开渲染
      try {
        sessionStorage.setItem('current_goods_summary', JSON.stringify(item));
        // 如果是在搜索态下跳转，立即持久化保存搜索快照及滚动条高度，保证手机返回时原样恢复
        const kw = homeSearchKeyword.value.trim();
        if (kw && homeGoodsList.value.length > 0) {
          syncHomeSearchToUrl(kw);
          sessionStorage.setItem('easy_shop_home_search_state', JSON.stringify({
            keyword: kw,
            list: homeGoodsList.value,
            page: homeSearchPage.value,
            offset: homeOffset.value,
            scrollTop: window.pageYOffset || document.documentElement.scrollTop || 0
          }));
        }
      } catch (e) {}

      const params = new URLSearchParams({
        goods_id: item.goods_id || item.product_id || '',
        goods_sign: item.goods_sign || '',
        platform: item.platform || 'pdd',
        uid: uid.value || '',
      });
      window.location.href = `/detail?${params.toString()}`;
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
          uid: (isLoggedIn.value && uid.value) ? uid.value : null, // 未登录传 null（自动命中/写入站长持久化缓存）
          title: currentGoods.value.name || '',
          price: currentGoods.value.price || 0,
          coupon_amount: currentGoods.value.coupon_amount || currentGoods.value.saved_price || 0,
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
            showToast('已激活并生成专属返利口令！');
          } else {
            showToast('已获取官方优惠口令与领券链接！');
          }
        } else {
          convertedLinkInfo.value = {
            final_link: currentGoods.value.product_url || '',
            command_text: `【好物精选】${currentGoods.value.name}\n【券后价】¥${currentGoods.value.price}\n【抢购口令】长按复制本段全部文字，打开【拼多多APP】立即领券下单！\n${currentGoods.value.product_url || ''}`,
            error: data.error_msg || '转链失败'
          };
          hasGeneratedLink.value = true;
        }
      } catch (e) {
        showToast('生成专属口令异常');
      } finally {
        isConverting.value = false;
      }
    };

    const handleJumpToBuy = async () => {
      if (!convertedLinkInfo.value || !convertedLinkInfo.value.final_link) {
        await handleGenerateLink();
      }
      if (convertedLinkInfo.value && convertedLinkInfo.value.final_link) {
        const copyTextContent = convertedLinkInfo.value.command_text || convertedLinkInfo.value.final_link;
        const targetUrl = convertedLinkInfo.value.final_link;
        const schemaUrl = convertedLinkInfo.value.schema_url;

        copyText(copyTextContent, '已复制抢购口令，正在前往购买...');
        setTimeout(() => {
          if (schemaUrl && navigator.userAgent.toLowerCase().includes('mobile')) {
            window.location.href = schemaUrl;
            setTimeout(() => {
              if (targetUrl) window.location.href = targetUrl;
            }, 800);
          } else {
            window.location.href = targetUrl;
          }
        }, 350);
      } else if (currentGoods.value && currentGoods.value.product_url) {
        window.location.href = currentGoods.value.product_url;
      } else {
        showToast('未获取到商品购买链接');
      }
    };

    const handleCopyPromoteLink = async () => {
      // 允许未登录用户生成口令和推广链接（未登录归属站长官方推广位，站长吃 100% 返利！）
      if (!convertedLinkInfo.value || !convertedLinkInfo.value.final_link) {
        showToast('正在生成抢购口令...');
        await handleGenerateLink();
      }

      if (convertedLinkInfo.value) {
        const copyContent = convertedLinkInfo.value.command_text || convertedLinkInfo.value.final_link;
        if (copyContent) {
          if (isLoggedIn.value && uid.value) {
            copyText(copyContent, `专属返利口令与链接已复制！推手: ${uid.value}，发送好友下单即可赚佣金！`);
          } else {
            copyText(copyContent, '抢购口令与领券链接已复制！打开拼多多APP即可自动识别，或通过链接直接下单~');
          }
          return;
        }
      }
      showToast('获取口令失败，请稍后重试');
    };

    const goToRegisterFromDetail = () => {
      isGoodsModalOpen.value = false;
      currentTab.value = 'user';
      isRegisterMode.value = true;
      showToast('开通返利钱包后可将购物补贴随时提现');
    };

    // ==================== 我的返利订单模块 ====================
    const syncAndQueryOrders = async (force = false) => {
      if (!isLoggedIn.value || !uid.value) {
        return;
      }
      const token = localStorage.getItem('easy_shop_token');
      if (!token) {
        return;
      }
      isSyncingOrders.value = true;
      try {
        const query = new URLSearchParams({
          platform: 'pdd',
          days: orderDays.value,
          force: force ? 'true' : 'false',
        });
        const res = await fetch(`/api/order/sync_and_query?${query.toString()}`, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${token}`
          }
        });
        const data = await res.json();
        if (data.status === 'success' && data.data) {
          userOrdersData.value = data.data;
          if (force) {
            showToast('已同步最近23小时官方订单！');
          }
        } else if (res.status === 401) {
          handleLogout();
          showToast('登录凭证已过期，请重新登录');
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

    const onTouchStart = (e) => {
      if (e.touches && e.touches[0]) {
        touchStartY = e.touches[0].clientY;
      }
    };

    const onTouchMove = (e) => {
      if (e.touches && e.touches[0]) {
        const currentY = e.touches[0].clientY;
        const deltaY = currentY - touchStartY;
        const scrollTop = window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0;

        // 用户手指往上滑 (deltaY < -10 且离开顶部) -> 立即平滑收起隐藏顶部栏
        if (deltaY < -10 && scrollTop > 30) {
          isHeaderHidden.value = true;
          touchStartY = currentY;
        } else if (deltaY > 10) {
          // 用户手指往下滑 (deltaY > 10) -> 立即滑出展现顶部栏
          isHeaderHidden.value = false;
          touchStartY = currentY;
        }
      }
    };

    const handleGlobalScroll = () => {
      const scrollTop = window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || (document.scrollingElement ? document.scrollingElement.scrollTop : 0);
      const scrollHeight = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, document.scrollingElement ? document.scrollingElement.scrollHeight : 0);
      const clientHeight = window.innerHeight || document.documentElement.clientHeight || document.body.clientHeight;

      // 1. 顶部栏智能显隐：根据滚动距离与方向判定
      if (scrollTop <= 15) {
        // 到达顶部强制常驻完全显示
        isHeaderHidden.value = false;
      } else {
        const diff = scrollTop - lastScrollTop;
        if (diff > 5 && scrollTop > 50) {
          // 页面向下滚动 (对应用户手指往上推)
          isHeaderHidden.value = true;
        } else if (diff < -5) {
          // 页面向上滚动 (对应用户手指往下拉)
          isHeaderHidden.value = false;
        }
      }
      lastScrollTop = Math.max(0, scrollTop);

      // 2. 距底部 800px 提前自动无缝触底加载下一页商品（大幅提前，保证极速丝滑无限流）
      if (scrollHeight - scrollTop - clientHeight < 800) {
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
      }, { rootMargin: '800px' });

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
      const urlTab = urlParams.get('tab');
      if (urlTab && ['home', 'boom', 'user'].includes(urlTab)) {
        currentTab.value = urlTab;
      }

      // 检查是否有搜索关键词（优先读取 URL，兜底读取 sessionStorage）
      const initialKeyword = (urlParams.get('keyword') || sessionStorage.getItem('easy_shop_home_search_keyword') || '').trim();

      let restoredSearch = false;
      if (initialKeyword && (!urlTab || urlTab === 'home')) {
        homeSearchKeyword.value = initialKeyword;
        syncHomeSearchToUrl(initialKeyword);
        try {
          const cachedSearch = sessionStorage.getItem('easy_shop_home_search_state');
          if (cachedSearch) {
            const parsed = JSON.parse(cachedSearch);
            if (parsed && parsed.keyword === initialKeyword && Array.isArray(parsed.list) && parsed.list.length > 0) {
              homeGoodsList.value = parsed.list;
              homeSearchPage.value = parsed.page || 2;
              homeOffset.value = parsed.offset || parsed.list.length;
              restoredSearch = true;
              setTimeout(() => {
                if (parsed.scrollTop) {
                  window.scrollTo(0, parsed.scrollTop);
                }
              }, 60);
            }
          }
        } catch (e) {}

        if (!restoredSearch) {
          loadHomeGoods(true);
        }
      } else {
        // 极速加载：立即并发拉取首屏推荐流
        loadHomeGoods(true);
      }

      loadHomeChannels();
      fetchUserInfo();

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
      clearHomeSearch,
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
      smartLoading,
      startSmartLoading,
      stopSmartLoading,
    };
  }
});

app.mount('#app');

