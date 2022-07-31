create or replace view tokenomics_ergopad as
	with 
	-- vested
	vested as (
		select (each(assets)).value::bigint as token_amount
		from utxos
		where ergo_tree = '10070400040204000500040004000400d80bd601e4c6a7040ed602b2db6308a7730000d6038c720201d604e4c6a70805d605e4c6a70705d606e4c6a70505d607e4c6a70605d6089c9d99db6903db6503fe720572067207d6098c720202d60a9972047209d60b958f99720472087207997204720a997208720ad1ed93b0b5a5d9010c63ededed93c2720c720193e4c6720c040ee4c6a7090e93b1db6308720c7301938cb2db6308720c7302000172037303d9010c41639a8c720c018cb2db63088c720c0273040002720bec937209720baea5d9010c63ededededededededed93c1720cc1a793c2720cc2a7938cb2db6308720c730500017203938cb2db6308720c73060002997209720b93e4c6720c040e720193e4c6720c0505720693e4c6720c0605720793e4c6720c0705720593e4c6720c0805720493e4c6720c090ee4c6a7090e'
	)
	-- emitted
	, emitted as (
		select (each(assets)).value::bigint as token_amount
		from utxos
		where ergo_tree = '102d04000e20d71693c49a84fbbecd4908c94813b46514b18b67a99952dc1e6e4791556de41304000e2005cde13424a7972fbcd0b43fccbb5e501b1f75302175178fc86d8f243f3f312504040404040604020400040204040402050004020400040004020402040404040400040004000402040004000e201028de73d018f0c9a374b71555c5b8f1390994f2f41633e7b9d68f77735782ee040004020100040604000500040204000400040204020400040204020404040404060100d802d601b2a4730000d602730195ed938cb2db6308720173020001730393c5b2a4730400c5a7d80ad603b2a5730500d604e4c672030411d605e4c672010411d606b27204730600d607b2a4730700d608b2e4c672070411730800d609db6308a7d60a9a8cb2db63087207730900029592b17209730a8cb27209730b0002730cd60bdb63087203d60cb2720b730d00d19683080193c27203c2a793b27204730e00b27205730f0093b27204731000b2720573110093b27204731200b27205731300937206958f7208720a7208720a938cb2720b731400018cb2720973150001938c720c017202938c720c0272069593c57201c5a7d80ad603b2a5731600d604db63087203d605db6308a7d6068cb2720573170002d607b5a4d9010763d801d609db630872079591b172097318ed938cb2720973190001731a93b2e4c672070411731b00b2e4c6a70411731c00731dd608e4c6a70411d609b27208731e00d60ab27208731f00d60bb072077320d9010b41639a8c720b019d9c8cb2db63088c720b02732100027209720ad60ce4c672030411d19683070193c27203c2a7938cb27204732200018cb272057323000195907206720b93b172047324d801d60db27204732500ed938c720d017202928c720d02997206720b93b2720c732600720a93b2720c732700b2720873280093b2720c73290099b27208732a007eb172070593b2720c732b007209d1732c'
	)
	-- staked
	, ergopad AS (
		select (u.assets -> 'd71693c49a84fbbecd4908c94813b46514b18b67a99952dc1e6e4791556de413'::varchar(64))::bigint AS amount,
		    (u.assets -> '1028de73d018f0c9a374b71555c5b8f1390994f2f41633e7b9d68f77735782ee'::varchar(64))::integer AS proxy,
			t.decimals
        from utxos u
        	join tokens t ON t.token_id::varchar(64) = 'd71693c49a84fbbecd4908c94813b46514b18b67a99952dc1e6e4791556de413'::varchar(64)
		where u.ergo_tree = '1017040004000e200549ea3374a36b7a22a803766af732e61798463c3332c5f6d86c8ab9195eed59040204000400040204020400040005020402040204060400040204040e2005cde13424a7972fbcd0b43fccbb5e501b1f75302175178fc86d8f243f3f312504020402010001010100d802d601b2a4730000d6028cb2db6308720173010001959372027302d80bd603b2a5dc0c1aa402a7730300d604e4c672030411d605e4c6a70411d606db63087203d607b27206730400d608db6308a7d609b27208730500d60ab27206730600d60bb27208730700d60c8c720b02d60de4c672010411d19683090193c17203c1a793c27203c2a793b272047308009ab27205730900730a93e4c67203050ee4c6a7050e93b27204730b00b27205730c00938c7207018c720901938c7207028c720902938c720a018c720b01938c720a029a720c9d9cb2720d730d00720cb2720d730e00d801d603b2a4730f009593c57203c5a7d801d604b2a5731000d1ed93720273119593c27204c2a7d801d605c67204050e95e67205ed93e47205e4c6a7050e938cb2db6308b2a573120073130001e4c67203050e73147315d17316'::text
	)
    , staked AS (
        select sum(amount) / power(10, decimals) AS amount
        from ergopad
        where proxy = 1		
        group by decimals
    )
	-- stake pool
	, stake_pool_assets as (
		select (each(assets)).value::bigint as token_amount
			, (each(assets)).key::varchar(64) as token_id
		from utxos 
		where ergo_tree = '1014040004000e2005cde13424a7972fbcd0b43fccbb5e501b1f75302175178fc86d8f243f3f312504020402040204040404040205000400040004000402040204000400040004000100d801d601b2a473000095ed938cb2db6308720173010001730293c5b2a4730300c5a7d808d602b2a5730400d603db63087202d604db6308a7d605b27204730500d606db6308b2a4730600d6079592b1720673078cb27206730800027309d6089a8c7205027207d609b2e4c6a70411730a00d19683050193c27202c2a7938cb27203730b00018cb27204730c0001959172087209d801d60ab27203730d00ed938c720a018c720501938c720a02997208720993b17203730e93b2e4c672020411730f00720993b2e4c6b2a57310000411731100999ab2e4c67201041173120072097207d17313'
	)
	, stake_pool as (
		select sum(token_amount) as amount
		from stake_pool_assets
		where token_id = 'd71693c49a84fbbecd4908c94813b46514b18b67a99952dc1e6e4791556de413'
	)

	select 
		(select sum(token_amount) from vested)::decimal(32, 10) as vested -- 317042346
		, (select sum(token_amount) from emitted)::decimal(32, 10) as emitted -- 1375
		, (select sum(amount) from staked)::decimal(32, 10) as staked -- 60423356.210000046
		, (select sum(amount) from stake_pool)::decimal(32, 10) as stake_pool -- 27851650278
;