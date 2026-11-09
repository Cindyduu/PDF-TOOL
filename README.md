PDF TOOL V3.0
更新如下：
1.下载时选择保存位置与改文件名
2.原计划写的是「零后端改动」，实测被推翻了：Chrome 里服务端 Content-Disposition 的 filename* 优先于 <a download>，只改前端的话，改名在真 Chrome 上根本不生效。所以给 GET /api/jobs/{id}/file 加了可选 name 参数，名字只进响应头、不参与任何路径拼接，不传时行为与改版前完全一致。

这个坑很隐蔽：Playwright 的 suggested_filename 会显示你改的名字，看着是通过的，但真浏览器落盘用的是服务端给的名字。
3.但是只有在点进单个文件时才能选位置，第四版继续改进
<img width="1908" height="951" alt="image" src="https://github.com/user-attachments/assets/8e72fdd5-e43b-4564-b76d-8771aed06383" />
4.拿到新的参考文件
